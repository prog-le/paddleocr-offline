"""运行设备探测与解析（CPU / NVIDIA GPU / 其它 Paddle 设备）。

设计目标
--------
1. **配置只写语义**：`ocr.device` 支持 `auto` / `cpu` / `gpu` / `gpu:1` / `xpu:0` / `npu:0`，
   由本模块解析成 Paddle 能接受的设备串，并在不可用时给出可操作的原因。
2. **Apple Silicon 明确只用 CPU**：PaddlePaddle 至今没有 Metal/MPS 后端
   （`paddle.device.is_compiled_with_cuda()` 为 False，也没有 mps place），
   所以 macOS arm64 上强制 CPU，并把原因、可调项（线程数）写进提示，避免用户以为"没生效"。
3. **探测不炸进程**：所有 paddle 调用都包在 try/except 里；没有 paddle 时也能返回
   CPU 信息（`verify_runtime.sh`、后台自检会用到）。
4. **单元可测**：`resolve()` 允许注入 probe，便于在无 GPU 机器上验证 GPU 分支。

显存口径说明
------------
容器/进程看到的"显存"有两层：
  * Paddle 显存池：`paddle.device.cuda.memory_allocated()`（张量）/
    `memory_reserved()`（池向驱动申请的）——这是本进程自己的占用；
  * 整卡：`nvidia-smi` 的 memory.total / memory.used——包含其它进程。
后台同时展示两者，排查 "GPU OOM" 时先看池 reserved，再看整卡 used。
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_SPEC_RE = re.compile(r"^\s*(auto|cpu|gpu|cuda|xpu|npu|mlu|mps|dcu)(?::(\d+))?\s*$", re.I)
_KNOWN_KINDS = {"auto", "cpu", "gpu", "cuda", "xpu", "npu", "mlu", "mps", "dcu"}


class DeviceError(RuntimeError):
    """设备不可用/配置非法。消息面向最终用户，包含修复建议。"""


@dataclass
class DeviceInfo:
    requested: str = "auto"
    device: str = "cpu"          # 实际传给 Paddle 的设备串
    kind: str = "cpu"            # cpu | gpu | xpu | npu | mps | ...
    index: int = 0
    name: str = "CPU"
    total_mem_mb: Optional[int] = None
    backend: str = "paddle CPU"
    fallback: bool = False       # 是否因不可用而降级
    reason: str = ""             # 降级/强制的原因（前端展示）
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "device": self.device,
            "kind": self.kind,
            "index": self.index,
            "name": self.name,
            "total_mem_mb": self.total_mem_mb,
            "backend": self.backend,
            "fallback": self.fallback,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------- 平台
def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64")


def platform_summary() -> Dict[str, Any]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "apple_silicon": is_apple_silicon(),
    }


# --------------------------------------------------------------------- Paddle
def _paddle() -> Any:
    try:
        import paddle  # noqa: F401

        return paddle
    except Exception:  # noqa: BLE001
        return None


def paddle_capabilities() -> Dict[str, Any]:
    """Paddle 编译能力（不加载模型，开销很小）。"""
    p = _paddle()
    if p is None:
        return {"installed": False, "version": None, "cuda": False, "xpu": False, "custom": []}
    custom: List[str] = []
    try:
        custom = list(p.device.get_all_custom_device_type() or [])
    except Exception:  # noqa: BLE001
        custom = []
    return {
        "installed": True,
        "version": getattr(p, "__version__", None),
        "cuda": bool(p.device.is_compiled_with_cuda()),
        "xpu": bool(getattr(p.device, "is_compiled_with_xpu", lambda: False)()),
        "custom": custom,
    }


def parse_spec(spec: str) -> Tuple[str, int]:
    """把配置值解析成 (kind, index)。非法值抛 DeviceError。"""
    raw = str(spec or "auto").strip()
    m = _SPEC_RE.match(raw)
    if not m:
        raise DeviceError(
            f"无法识别的设备配置：{raw!r}。可用写法：auto / cpu / gpu / gpu:0 / gpu:1 / xpu:0 / npu:0"
        )
    kind = m.group(1).lower()
    if kind == "cuda":
        kind = "gpu"
    index = int(m.group(2) or 0)
    return kind, index


def _gpu_props(index: int) -> Tuple[Optional[str], Optional[int]]:
    p = _paddle()
    if p is None:
        return None, None
    try:
        props = p.device.cuda.get_device_properties(index)
        name = getattr(props, "name", None) or str(props)
        total = getattr(props, "total_memory", None)
        if isinstance(total, int) and total > 0:
            # Paddle 返回字节；极少数版本返回 MB，用阈值兜底
            total_mb = total // (1024 * 1024) if total > 1024 * 1024 else total
        else:
            total_mb = None
        return str(name), total_mb
    except Exception:  # noqa: BLE001
        return None, None


def gpu_list() -> List[Dict[str, Any]]:
    """列出可用 GPU（无 CUDA 时返回空列表）。"""
    p = _paddle()
    if p is None:
        return []
    try:
        if not p.device.is_compiled_with_cuda():
            return []
        count = int(p.device.cuda.device_count() or 0)
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, Any]] = []
    for i in range(count):
        name, total = _gpu_props(i)
        out.append({"index": i, "name": name or f"GPU {i}", "total_mem_mb": total})
    return out


def probe(kind: str, index: int = 0) -> Tuple[bool, str, str, Optional[int]]:
    """探测设备是否可用，返回 (可用, 设备名, 后端说明, 总显存MB)。"""
    p = _paddle()
    if kind == "cpu":
        return True, "CPU", "paddle CPU", None
    if kind == "mps":
        return (
            False,
            "Apple Silicon GPU (Metal/MPS)",
            "Paddle 无 Metal 后端",
            None,
        )
    if p is None:
        return False, kind, "paddle 未安装", None
    try:
        if kind == "gpu":
            if not p.device.is_compiled_with_cuda():
                return False, "GPU", "当前 paddle 为 CPU 版（无 CUDA）", None
            count = int(p.device.cuda.device_count() or 0)
            if count <= index:
                return False, "GPU", f"只检测到 {count} 张 GPU，索引 {index} 越界", None
            name, total = _gpu_props(index)
            return True, name or f"GPU {index}", "paddle CUDA", total
        if kind == "xpu":
            ok = bool(getattr(p.device, "is_compiled_with_xpu", lambda: False)())
            return ok, "Intel XPU", "paddle XPU" if ok else "当前 paddle 未编译 XPU", None
        if kind in ("npu", "mlu", "dcu"):
            caps = paddle_capabilities()
            ok = kind in (caps.get("custom") or [])
            return ok, kind.upper(), f"custom device ({kind})" if ok else f"当前 paddle 不支持 {kind}", None
    except Exception as exc:  # noqa: BLE001
        return False, kind, f"探测异常：{exc}", None
    return False, kind, "未知设备类型", None


def resolve(spec: str = "auto", *, prober=probe, apple_silicon: Optional[bool] = None) -> DeviceInfo:
    """把配置值解析成实际可用设备；`auto` 优先 GPU，其次 CPU。

    `prober` 可注入（单测用），签名同 `probe`。
    """
    requested = str(spec or "auto").strip() or "auto"
    kind, index = parse_spec(requested)
    is_apple = is_apple_silicon() if apple_silicon is None else apple_silicon

    # macOS / Apple Silicon：Paddle 只有 CPU 内核，显式拒绝 GPU 请求并说明原因
    if is_apple and kind in ("gpu", "mps", "xpu", "npu", "mlu", "dcu"):
        ok, name, backend, total = prober("cpu", 0)
        return DeviceInfo(
            requested=requested,
            device="cpu",
            kind="cpu",
            index=0,
            name=name,
            total_mem_mb=total,
            backend=backend,
            fallback=True,
            reason=(
                "Apple Silicon 上 PaddlePaddle 没有 Metal/MPS 后端，只能用 CPU 推理"
                f"（配置的是 {requested}）。已自动切换为 CPU。"
            ),
            warnings=[
                "macOS 上想更快：调大 ocr.cpu_threads（如 8）并降低 pdf_dpi/max_side_px；"
                "需要 GPU 加速请部署到 NVIDIA 显卡的 Linux 主机。",
            ],
        )

    # Apple Silicon 上连探测都省掉：Paddle 只有 CPU 内核，auto 直接定 CPU
    if is_apple and kind == "auto":
        ok, name, backend, total = prober("cpu", 0)
        return DeviceInfo(
            requested=requested,
            device="cpu",
            kind="cpu",
            index=0,
            name=name,
            total_mem_mb=total,
            backend=backend,
            fallback=False,
            reason="Apple Silicon 上没有可用的 GPU 后端（Paddle 无 Metal/MPS），以 CPU 运行",
            warnings=[
                "可调大 ocr.cpu_threads（如 8）并降低 pdf_dpi/max_side_px 提速；"
                "需要 GPU 加速请部署到 NVIDIA 显卡的 Linux 主机。",
            ],
        )

    if kind == "auto":
        # 顺序：NVIDIA GPU → Intel XPU → 华为 NPU → CPU。
        # 直接交给 prober 判断，避免先读 paddle 编译能力（也便于单测注入）。
        candidates: List[Tuple[str, int]] = [("gpu", 0), ("xpu", 0), ("npu", 0), ("cpu", 0)]
        for cand_kind, cand_index in candidates:
            ok, name, backend, total = prober(cand_kind, cand_index)
            if ok:
                return DeviceInfo(
                    requested=requested,
                    device="cpu" if cand_kind == "cpu" else f"{cand_kind}:{cand_index}",
                    kind=cand_kind,
                    index=cand_index,
                    name=name,
                    total_mem_mb=total,
                    backend=backend,
                    fallback=False,
                    reason="" if cand_kind != "cpu" else "未检测到可用加速卡，使用 CPU",
                )
        # 连 CPU 都探测失败（理论上不会）：仍然返回 CPU 让上层报加载错误
        return DeviceInfo(requested=requested, device="cpu", name="CPU", backend="paddle CPU")

    ok, name, backend, total = prober(kind, index)
    if not ok:
        hint = "可把 ocr.device 设为 auto 让服务自动选择，或设为 cpu。"
        if kind == "gpu" and not paddle_capabilities().get("cuda"):
            hint = (
                "当前安装的是 CPU 版 paddlepaddle。要用 NVIDIA GPU 需改装 GPU 版：\n"
                "  ./scripts/download_wheels.sh --arch amd64 --device gpu --cuda cu126\n"
                "  ./scripts/install_offline.sh --device gpu\n"
                "并确保宿主机 NVIDIA 驱动与 CUDA 版本匹配。"
            )
        raise DeviceError(f"设备 {requested} 不可用：{backend}。{hint}")

    return DeviceInfo(
        requested=requested,
        device="cpu" if kind == "cpu" else f"{kind}:{index}",
        kind=kind,
        index=index,
        name=name,
        total_mem_mb=total,
        backend=backend,
    )


# --------------------------------------------------------------------- 显存
_SMI_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


def nvidia_smi(timeout: float = 2.0) -> List[Dict[str, Any]]:
    """整卡显存（nvidia-smi，2 秒缓存）。容器里无该命令时返回空列表。"""
    now = time.time()
    if _SMI_CACHE["data"] is not None and now - _SMI_CACHE["at"] < 2:
        return _SMI_CACHE["data"]
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        rows: List[Dict[str, Any]] = []
        for line in (out.stdout or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                rows.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "total_mem_mb": int(float(parts[2])),
                        "used_mem_mb": int(float(parts[3])),
                        "free_mem_mb": int(float(parts[4])),
                        "util_percent": int(float(parts[5])) if len(parts) > 5 and parts[5] else None,
                    }
                )
            except ValueError:
                continue
        _SMI_CACHE.update(at=now, data=rows)
        return rows
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        _SMI_CACHE.update(at=now, data=[])
        return []


def gpu_memory(index: int = 0) -> Dict[str, Any]:
    """本进程的 Paddle 显存池 + 整卡占用。无 GPU 时返回空字典。"""
    p = _paddle()
    if p is None:
        return {}
    try:
        if not p.device.is_compiled_with_cuda():
            return {}
        mb = 1024 * 1024

        def _val(fn: str) -> Optional[int]:
            try:
                return int(getattr(p.device.cuda, fn)(index) // mb)
            except Exception:  # noqa: BLE001
                return None

        info: Dict[str, Any] = {
            "index": index,
            "allocated_mb": _val("memory_allocated"),
            "reserved_mb": _val("memory_reserved"),
            "peak_allocated_mb": _val("max_memory_allocated"),
            "peak_reserved_mb": _val("max_memory_reserved"),
        }
        rows = nvidia_smi()
        card = next((r for r in rows if r.get("index") == index), None)
        if card:
            info.update(
                {
                    "name": card.get("name"),
                    "total_mem_mb": card.get("total_mem_mb"),
                    "used_mem_mb": card.get("used_mem_mb"),
                    "free_mem_mb": card.get("free_mem_mb"),
                    "util_percent": card.get("util_percent"),
                    "source": "nvidia-smi",
                }
            )
        else:
            _, total = _gpu_props(index)
            info.update({"total_mem_mb": total, "source": "paddle"})
        return {k: v for k, v in info.items() if v is not None}
    except Exception:  # noqa: BLE001
        return {}


def reset_peak_memory(index: int = 0) -> None:
    p = _paddle()
    if p is None:
        return
    try:
        if p.device.is_compiled_with_cuda():
            p.device.cuda.reset_peak_memory_stats(index)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------- 运行时
def apply_gpu_memory_limit(index: int, limit_mb: int = 0, fraction: float = 0.0) -> Optional[float]:
    """设置显存池上限，返回实际使用的 fraction（None 表示未设置）。

    * `fraction` > 0 直接生效；
    * 否则用 `limit_mb / 卡总显存` 换算；
    * Paddle 的显存池按 fraction 预留，**不是硬上限**，超出仍会 OOM；
      真正的保护是 `ocr.text_rec_batch_size` 与单页像素上限。
    """
    p = _paddle()
    if p is None or not p.device.is_compiled_with_cuda():
        return None
    frac: Optional[float] = None
    if fraction and 0 < float(fraction) <= 0.95:
        frac = float(fraction)
    elif limit_mb and limit_mb > 0:
        _, total = _gpu_props(index)
        if total:
            frac = max(0.05, min(0.95, float(limit_mb) / float(total)))
    if frac is None:
        return None
    try:
        p.set_flags({"FLAGS_fraction_of_gpu_memory_to_use": frac})
    except Exception:  # noqa: BLE001
        return None
    os.environ["FLAGS_fraction_of_gpu_memory_to_use"] = f"{frac:.3f}"
    return frac


def pre_import_env(settings: Any) -> None:
    """在 import paddle 之前能设的显存/线程环境变量。"""
    try:
        frac = float(settings.get("ocr.gpu_mem_fraction", 0) or 0)
    except (TypeError, ValueError):
        frac = 0.0
    if 0 < frac <= 0.95:
        os.environ.setdefault("FLAGS_fraction_of_gpu_memory_to_use", f"{frac:.3f}")
    # 关掉 Paddle 的显存碎片整理日志噪音（不影响行为）
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")


def summary(settings: Any) -> Dict[str, Any]:
    """给 /api/meta、/api/health、后台用的设备总览（不加载模型）。"""
    spec = str(settings.get("ocr.device", "auto") or "auto")
    info: Dict[str, Any]
    try:
        info = resolve(spec).to_dict()
    except DeviceError as exc:
        info = {
            "requested": spec,
            "device": "cpu",
            "kind": "cpu",
            "name": "CPU",
            "fallback": True,
            "reason": str(exc),
            "warnings": [],
        }
    data: Dict[str, Any] = {
        "platform": platform_summary(),
        "capabilities": paddle_capabilities(),
        "configured": spec,
        "active": info,
        "gpu_count": len(gpu_list()),
        "gpus": gpu_list(),
        "batching": {
            "text_rec_batch_size": settings.get("ocr.text_rec_batch_size"),
            "textline_batch_size": settings.get("ocr.textline_batch_size"),
        },
        "gpu_mem_limit_mb": settings.get("ocr.gpu_mem_limit_mb"),
        "gpu_mem_fraction": settings.get("ocr.gpu_mem_fraction"),
        "cpu_threads": settings.get("ocr.cpu_threads"),
        "ocr_tier": settings.get("ocr.ocr_tier"),
    }
    if info.get("kind") == "gpu":
        data["memory"] = gpu_memory(int(info.get("index") or 0))
    return data


def describe(settings: Any) -> str:
    """一行摘要，写日志用。"""
    try:
        info = resolve(str(settings.get("ocr.device", "auto") or "auto"))
    except DeviceError as exc:
        return f"设备不可用（{exc}）"
    if info.kind == "gpu":
        mem = gpu_memory(info.index)
        extra = ""
        if mem.get("total_mem_mb"):
            extra = f" 显存 {mem.get('total_mem_mb')}MB"
        return f"{info.device} · {info.name}{extra}"
    if info.fallback:
        return f"{info.device} · {info.name}（{info.reason}）"
    return f"{info.device} · {info.name}"
