#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内存观测/保护单元测试（不依赖容器，用临时目录模拟 cgroup）。

覆盖：
  * cgroup v2 / v1 限额解析（含 "max" 与超大数当无限）
  * snapshot 的 free 计算口径
  * estimate_page_mb 的像素估算
  * ensure_headroom 在余量不足时抛 MemError、充足时放行
  * min_free_mem_mb=0 时不做检查

    python3 tests/test_memguard.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import memguard  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name} {detail}")


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_cgroup_v2() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "v2"
        write(root / "memory.max", str(8 * 1024**3))
        write(root / "memory.current", str(int(6.5 * 1024**3)))
        info = memguard.cgroup_memory(v2_root=root, v1_root=Path(tmp) / "none")
        check("v2：识别为 cgroup-v2", info["source"] == "cgroup-v2", str(info))
        check("v2：限额 8192MB", info["limit_mb"] == 8192.0, str(info["limit_mb"]))
        check("v2：已用 6656MB", abs(info["used_mb"] - 6656.0) < 2, str(info["used_mb"]))


def test_cgroup_unlimited() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "v2"
        write(root / "memory.max", "max")
        write(root / "memory.current", str(1024**3))
        info = memguard.cgroup_memory(v2_root=root, v1_root=Path(tmp) / "none")
        check("v2：max 视为无限", info["limit_mb"] is None and info["available"] is True, str(info))


def test_cgroup_v1() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        v2 = Path(tmp) / "v2empty"
        v2.mkdir()
        v1 = Path(tmp) / "v1"
        write(v1 / "memory.limit_in_bytes", str(4 * 1024**3))
        write(v1 / "memory.usage_in_bytes", str(2 * 1024**3))
        info = memguard.cgroup_memory(v2_root=v2, v1_root=v1)
        check("v1：回退到 cgroup-v1", info["source"] == "cgroup-v1", str(info))
        check("v1：限额 4096MB", info["limit_mb"] == 4096.0, str(info))


def test_v1_unlimited_sentinel() -> None:
    """v1 用 9223372036854771712 表示无限，必须当成 None，否则算出来的“可用”是假的。"""
    with tempfile.TemporaryDirectory() as tmp:
        v2 = Path(tmp) / "v2empty"
        v2.mkdir()
        v1 = Path(tmp) / "v1"
        write(v1 / "memory.limit_in_bytes", "9223372036854771712")
        write(v1 / "memory.usage_in_bytes", str(1024**3))
        info = memguard.cgroup_memory(v2_root=v2, v1_root=v1)
        check("v1：无限哨兵值被识别为 None", info["limit_mb"] is None, str(info))


def test_estimate() -> None:
    small = memguard.estimate_page_mb(200, 200)
    big = memguard.estimate_page_mb(2400, 3400)
    a4 = memguard.estimate_page_mb(1654, 2339)
    check("估算：小图约等于固定开销（600~700MB）", 600 <= small <= 700, str(small))
    check("估算：A4@200dpi 约 1.2~2GB", 1200 < a4 < 2000, str(a4))
    check("估算：2400×3400 约 2.5~3GB", 2400 < big < 3100, str(big))
    check("估算：异常入参不炸", 600 <= memguard.estimate_page_mb(0, 0) <= 700, "")


def test_headroom_with_cgroup() -> None:
    """用 monkeypatch 替换 cgroup 读取，验证 ensure_headroom 的放行/拦截。"""
    orig = memguard.cgroup_memory
    try:
        memguard.cgroup_memory = lambda **_: {  # type: ignore[assignment]
            "available": True,
            "source": "cgroup-v2",
            "limit_mb": 8192.0,
            "used_mb": 7800.0,
        }
        info = memguard.snapshot()
        check("快照：free = limit - used", abs(info["free_mb"] - 392.0) < 1, str(info))
        try:
            memguard.ensure_headroom(1500, 600)
        except memguard.MemError as exc:
            msg = str(exc)
            check("余量不足：抛 MemError", True)
            check("提示里含 MEM_LIMIT 建议", "MEM_LIMIT" in msg, msg[:80])
            check("提示里含 pdf_dpi 建议", "pdf_dpi" in msg, msg[:80])
        else:
            check("余量不足：抛 MemError", False, "没有抛出")

        memguard.cgroup_memory = lambda **_: {  # type: ignore[assignment]
            "available": True,
            "source": "cgroup-v2",
            "limit_mb": 16384.0,
            "used_mb": 4096.0,
        }
        try:
            memguard.ensure_headroom(2000, 600)
            check("余量充足：放行", True)
        except memguard.MemError:
            check("余量充足：放行", False, "误判为不足")

        # 0 表示关闭检查
        memguard.cgroup_memory = lambda **_: {  # type: ignore[assignment]
            "available": True,
            "source": "cgroup-v2",
            "limit_mb": 2048.0,
            "used_mb": 2000.0,
        }
        try:
            memguard.ensure_headroom(0, 0)
            check("need=0：不做检查", True)
        except memguard.MemError:
            check("need=0：不做检查", False, "不应抛错")
    finally:
        memguard.cgroup_memory = orig  # type: ignore[assignment]


def test_format() -> None:
    text = memguard.format_snapshot(
        {"rss_mb": 1234.0, "peak_mb": 1500.0, "limit_mb": 8192.0, "used_mb": 3000.0, "free_mb": 5192.0}
    )
    check("摘要包含 RSS/限额/可用", all(k in text for k in ("RSS", "限额", "可用")), text)


def main() -> int:
    print("== cgroup 解析 ==")
    test_cgroup_v2()
    test_cgroup_unlimited()
    test_cgroup_v1()
    test_v1_unlimited_sentinel()
    print("== 估算与保护 ==")
    test_estimate()
    test_headroom_with_cgroup()
    test_format()
    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
