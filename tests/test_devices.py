"""设备解析单测（不需要 GPU / 模型）。

覆盖：
  * 配置串解析（auto/cpu/gpu/gpu:1/xpu:0/非法值）
  * auto 在有卡/无卡时的选择
  * Apple Silicon 强制 CPU（即使配置 gpu / mps）
  * 指定 GPU 但不可用时的报错文案（含安装指引）
  * 显存换算的比例边界
  * /api/meta 用的 summary 结构
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import devices  # noqa: E402

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


def fake_prober(available: Dict[str, Any], name: str = "Fake GPU", total: Optional[int] = 24576):
    """构造一个只认识给定设备的 prober。"""

    def _probe(kind: str, index: int = 0):
        if kind in available:
            ok = available[kind] if isinstance(available[kind], bool) else True
            if not ok:
                return False, name, "不可用", None
            return True, name if kind != "cpu" else "CPU", f"fake-{kind}", total if kind == "gpu" else None
        return False, name, "不支持的设备类型", None

    return _probe


class FakeSettings:
    def __init__(self, **kw: Any) -> None:
        self.data = {"device": "auto", "text_rec_batch_size": 6, "textline_batch_size": 6,
                     "gpu_mem_limit_mb": 0, "gpu_mem_fraction": 0.0, "ocr_tier": "medium",
                     "cpu_threads": 0}
        self.data.update(kw)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key.split(".")[-1], default)


def main() -> int:
    print("设备解析：parse_spec")
    check("auto", devices.parse_spec("auto") == ("auto", 0), str(devices.parse_spec("auto")))
    check("gpu:1", devices.parse_spec("gpu:1") == ("gpu", 1), str(devices.parse_spec("gpu:1")))
    check("cuda 归一为 gpu", devices.parse_spec("cuda:2") == ("gpu", 2), str(devices.parse_spec("cuda:2")))
    check("大小写不敏感", devices.parse_spec("GPU:0") == ("gpu", 0), str(devices.parse_spec("GPU:0")))
    check("空值视为 auto", devices.parse_spec("") == ("auto", 0), str(devices.parse_spec("")))
    for bad in ("gpu:abc", "tpu", "gpu:"):
        try:
            devices.parse_spec(bad)
            check(f"非法值 {bad!r} 应报错", False)
        except devices.DeviceError:
            check(f"非法值 {bad!r} 报错", True)

    print("\n设备解析：auto")
    # 有 CUDA → 选 gpu:0
    info = devices.resolve("auto", prober=fake_prober({"gpu": True, "cpu": True}), apple_silicon=False)
    check("auto 有 GPU 时选 gpu:0", info.device == "gpu:0" and info.kind == "gpu", info.device)
    check("auto 有 GPU 时记录显存", info.total_mem_mb == 24576, str(info.total_mem_mb))
    # 无 CUDA → cpu
    info = devices.resolve("auto", prober=fake_prober({"gpu": False, "cpu": True}), apple_silicon=False)
    check("auto 无 GPU 时回落 CPU", info.device == "cpu" and not info.fallback, info.device)
    check("auto 回落 CPU 有原因说明", "未检测到" in info.reason, info.reason)

    print("\n设备解析：Apple Silicon")
    for spec in ("gpu", "gpu:0", "mps", "auto"):
        info = devices.resolve(spec, prober=fake_prober({"cpu": True}), apple_silicon=True)
        check(f"{spec} 在 Apple Silicon 上强制 CPU", info.device == "cpu" and info.kind == "cpu", info.device)
    info = devices.resolve("gpu", prober=fake_prober({"cpu": True}), apple_silicon=True)
    check("Apple Silicon 配置 gpu 有明确提示", info.fallback and "Metal" in info.reason, info.reason)
    check("Apple Silicon 给出优化建议", any("cpu_threads" in w for w in info.warnings), str(info.warnings))
    # auto 在 Apple Silicon 上不探测 GPU（避免误选）
    calls = []
    base = fake_prober({"gpu": True, "cpu": True})

    def spy(kind, index=0):
        calls.append(kind)
        return base(kind, index)

    devices.resolve("auto", prober=spy, apple_silicon=True)
    check("Apple Silicon auto 不探测 GPU", "gpu" not in calls, str(calls))

    print("\n设备解析：显式指定但不可用")
    try:
        devices.resolve("gpu:0", prober=fake_prober({"gpu": False, "cpu": True}), apple_silicon=False)
        check("GPU 不可用应报错", False)
    except devices.DeviceError as exc:
        msg = str(exc)
        check("GPU 不可用报错含设备名", "gpu:0" in msg, msg)
        check("GPU 不可用报错含安装指引", "download_wheels" in msg and "--device gpu" in msg, msg)
    try:
        devices.resolve("xpu:0", prober=fake_prober({"cpu": True}), apple_silicon=False)
        check("XPU 不可用应报错", False)
    except devices.DeviceError as exc:
        check("XPU 不可用报错含 auto 建议", "auto" in str(exc), str(exc))

    print("\n显存与摘要")
    check("无 paddle 时 gpu_list 为空", isinstance(devices.gpu_list(), list))
    check("无 GPU 时 gpu_memory 为空字典", devices.gpu_memory(0) == {} or isinstance(devices.gpu_memory(0), dict))
    frac_ok = devices.apply_gpu_memory_limit(0, limit_mb=0, fraction=0)
    check("未启用 GPU 时不设置显存比例", frac_ok is None, str(frac_ok))

    summary = devices.summary(FakeSettings(device="cpu"))
    check("summary 含 active", summary.get("active", {}).get("device") == "cpu", str(summary)[:120])
    check("summary 含 batching", summary["batching"]["text_rec_batch_size"] == 6, str(summary["batching"]))
    check("summary 含平台信息", "apple_silicon" in summary["platform"], str(summary["platform"]))
    check("summary 含 paddle 能力", "cuda" in summary["capabilities"], str(summary["capabilities"]))

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
