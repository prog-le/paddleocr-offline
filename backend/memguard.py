"""内存观测与保护。

容器里最常见的“服务跑一会儿自己重启”其实是 **cgroup 内存超限被 OOM kill**，
再由 compose 的 `restart: unless-stopped` 拉起 —— 从外部看就是“自动重启”。

`psutil.virtual_memory()` 在容器内看到的是**宿主机**内存，不能用来判断是否快被
cgroup 杀掉，所以这里优先读 cgroup（v2 / v1）的 limit 与 current：

    v2: /sys/fs/cgroup/memory.max   + memory.current
    v1: /sys/fs/cgroup/memory/memory.limit_in_bytes + memory.usage_in_bytes

对外提供三件事：
  * snapshot()        —— 当前 RSS / cgroup 限额 / 已用 / 可用
  * ensure_headroom() —— 单页推理前检查余量，不足时抛 MemError（任务失败但容器存活）
  * 每页与任务结束时的日志，便于看出内存是否随页数增长
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:  # psutil 已在依赖里，缺失时只降级为 cgroup 数据
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

CGROUP_V2_ROOT = Path(os.environ.get("MEMGUARD_CGROUP_V2", "/sys/fs/cgroup"))
CGROUP_V1_ROOT = Path(os.environ.get("MEMGUARD_CGROUP_V1", "/sys/fs/cgroup/memory"))

# “无限”在 cgroup 里是一个很大的数（v1 常见 9223372036854771712）
_UNLIMITED_THRESHOLD = 1 << 50  # 1 PiB

_lock = threading.Lock()
_peak_rss_mb: float = 0.0


class MemError(RuntimeError):
    """内存余量不足（消息可直接展示给用户）。"""


def _read_int(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _mb(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    if value >= _UNLIMITED_THRESHOLD:
        return None
    return round(value / 1024 / 1024, 1)


def cgroup_memory(
    v2_root: Path = CGROUP_V2_ROOT, v1_root: Path = CGROUP_V1_ROOT
) -> Dict[str, Any]:
    """读取 cgroup 内存信息；非容器环境返回 available=False。"""
    # ---- cgroup v2 ----
    limit = _read_int(v2_root / "memory.max")
    used = _read_int(v2_root / "memory.current")
    source = "cgroup-v2"
    if limit is None and used is None:
        # ---- cgroup v1 ----
        limit = _read_int(v1_root / "memory.limit_in_bytes")
        used = _read_int(v1_root / "memory.usage_in_bytes")
        source = "cgroup-v1"
    if limit is None and used is None:
        return {"available": False, "source": "none", "limit_mb": None, "used_mb": None}
    return {
        "available": True,
        "source": source,
        "limit_mb": _mb(limit),
        "used_mb": _mb(used),
    }


def process_rss_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def _host_available_mb() -> Optional[float]:
    if psutil is None:
        return None
    try:
        # 容器内 cap 过的 meminfo 才是容器可用量，宿主机数值只作兜底
        return round(psutil.virtual_memory().available / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def snapshot() -> Dict[str, Any]:
    """当前内存快照：RSS、cgroup 限额/已用/可用、峰值。"""
    rss = process_rss_mb()
    cg = cgroup_memory()
    limit_mb = cg.get("limit_mb")
    used_mb = cg.get("used_mb")
    if limit_mb is not None and used_mb is not None:
        # cgroup 口径更接近“容器还能用多少”，优先用它
        free_mb: Optional[float] = round(max(limit_mb - used_mb, 0.0), 1)
        scope = "container"
    else:
        free_mb = _host_available_mb()
        scope = "host"
    peak = _peak()
    return {
        "rss_mb": rss,
        "peak_mb": peak,
        "limit_mb": limit_mb,
        "used_mb": used_mb,
        "free_mb": free_mb,
        "scope": scope,
        "source": cg.get("source"),
    }


def _peak() -> Optional[float]:
    with _lock:
        return round(_peak_rss_mb, 1) if _peak_rss_mb else None


def note_rss() -> Optional[float]:
    """采样一次 RSS 并更新峰值（每页调用）。"""
    global _peak_rss_mb
    rss = process_rss_mb()
    if rss is None:
        return None
    with _lock:
        if rss > _peak_rss_mb:
            _peak_rss_mb = rss
    return rss


def reset_peak() -> None:
    global _peak_rss_mb
    with _lock:
        _peak_rss_mb = 0.0


def format_snapshot(info: Optional[Dict[str, Any]] = None) -> str:
    """一行可读摘要，用于日志。"""
    info = info or snapshot()
    parts = []
    if info.get("rss_mb") is not None:
        parts.append(f"RSS {info['rss_mb']:.0f}MB")
    if info.get("limit_mb") is not None:
        parts.append(f"限额 {info['limit_mb']:.0f}MB")
        parts.append(f"容器已用 {info['used_mb']:.0f}MB")
    if info.get("free_mb") is not None:
        parts.append(f"可用 {info['free_mb']:.0f}MB")
    if info.get("peak_mb"):
        parts.append(f"峰值 {info['peak_mb']:.0f}MB")
    return " / ".join(parts) if parts else "内存信息不可用"


def ensure_headroom(required_mb: int, min_free_mb: int = 0) -> Dict[str, Any]:
    """确认还有足够余量再开始下一页推理。

    判定：可用内存 < max(required_mb, min_free_mb) 时抛 MemError。
    抛出后任务会失败并给出可操作建议，**不会把容器拖到 OOM kill**。

    说明：`required_mb` 是这一页推理的预估峰值（由调用方按像素估算），
    `min_free_mb` 是留给系统与页缓存的缓冲（配置项 limits.min_free_mem_mb）。
    """
    info = snapshot()
    need = max(int(required_mb or 0), int(min_free_mb or 0))
    free = info.get("free_mb")
    if need <= 0 or free is None:
        return info
    if free < need:
        scope = "容器" if info.get("scope") == "container" else "系统"
        limit = info.get("limit_mb")
        hint = (
            f"{scope}可用内存不足：需要约 {need:.0f}MB，当前只剩 {free:.0f}MB"
            f"（RSS {info.get('rss_mb') or 0:.0f}MB"
            + (f"，容器上限 {limit:.0f}MB" if limit else "")
            + "）。\n"
            "可采取措施（任选）：\n"
            "  1) 增大容器内存上限：docker/.env 里 MEM_LIMIT（如 8g/12g）后 `docker compose up -d`；\n"
            "  2) 降低单页像素：config.yaml → ocr.pdf_dpi: 150、ocr.max_side_px: 1600、"
            "limits.max_image_pixels: 20000000；\n"
            "  3) 减少同时加载的产线：后台「服务控制 → 卸载」，只保留常用的 1~2 条；\n"
            "  4) 拆分大文件：把一个大 PDF 按页拆成几个小文件分别识别。"
        )
        raise MemError(hint)
    return info


def estimate_page_mb(width: int, height: int, bytes_per_pixel: int = 256) -> int:
    """按图像像素估算单页推理的峰值内存需求（MB）。

    标定依据：本机 A4@200dpi（1654×2339 ≈ 3.9MP）跑 PP-StructureV3，
    整进程 RSS 从 ~1.6GB（仅加载模型）涨到 ~3.5GB，即单页增量约 1.5~2GB。
    折算下来约 300~500 B/像素，这里取 **256 B/像素 + 600MB 固定开销**（偏保守但不过度吓人）：
        3.9MP  → 约 1.5 ~ 1.6GB
        8.2MP  → 约 2.6GB（2400×3400，max_side_px 上限附近）
    小图也有模型/框架的固定开销，所以下限 600MB。

    注意：这是"是否可能撑爆内存"的粗判据，不是精确值；宁可提前把任务判失败并提示用户
    调参，也不要把整个容器拖到 OOM kill。
    """
    try:
        pixels = max(int(width), 0) * max(int(height), 0)
    except (TypeError, ValueError):
        pixels = 0
    estimate = 600 + pixels * max(int(bytes_per_pixel), 1) / 1024 / 1024
    return int(estimate)
