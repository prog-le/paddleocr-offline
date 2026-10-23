#!/usr/bin/env python3
"""任务执行器在 **GPU 路径** 下的回归测试（不需要显卡、不需要模型）。

线上真实故障（amd64 + RTX 3080，目标机）：
    File "backend/manager.py", line 458, in _run
        f"{len(page.blocks)} 块/{len(page.tables)} 表", page_result.elapsed_ms, page_mem)
    UnboundLocalError: cannot access local variable 'page_mem'

原因：`page_mem` 只在 CPU 分支（`else`）里赋值，GPU 分支只写文件、不赋值，
后面那行无条件日志一引就炸 —— 于是**只要装了显卡，每一页都失败**，
而 CPU / MOCK 环境完全测不到。

本测试用一个假的 GPU 引擎把 `_run` 真正跑一遍（含 submit/线程池/日志文件）：
  1. GPU 路径能完整跑完一页，任务状态 done（旧代码在这里必挂）；
  2. 任务日志里出现显存信息（说明日志片段确实来自 GPU 分支）；
  3. `gpu_memory()` 抛异常时也不影响任务（日志降级为"显存信息不可用"）；
  4. `_page_mem_text()` 两个分支都必须返回字符串。

用法：python3 tests/test_manager_gpu_path.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


def make_png(path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), "GPU path regression", fill="black")
    d.line([(40, 120), (540, 120)], fill="black", width=2)
    img.save(path)


class FakeGpuEngine:
    """最小可用的引擎替身：device_info 声称在用 GPU。"""

    is_mock = True
    pipeline_name = "OCR"
    default_id = "OCR"
    label = "纯文字识别（PP-OCRv6）"
    device_info = {"kind": "gpu", "index": 0, "device": "gpu:0", "name": "Fake RTX 3080"}

    def ensure_loaded(self) -> None:
        pass

    def predict_page(self, path, params):
        from backend.docmodel import Block
        from backend.engine import PageResult

        return PageResult(
            blocks=[Block(type="text", label="text", text="hello gpu", bbox=[1, 2, 3, 4])],
            width=600,
            height=400,
            elapsed_ms=7,
            raw={},
        )


def run_task(tmp: Path, *, gpu_mem_raises: bool = False) -> tuple:
    """建一个独立数据目录的 TaskManager，提交一张图，等它结束。"""
    os.environ["PADDLEX_OCR_DATA_DIR"] = str(tmp / "data")
    os.environ["PADDLEX_OCR_LOG_DIR"] = str(tmp / "logs")
    os.environ["PADDLEX_OCR_MOCK"] = "1"

    from backend import devices as device_mod
    from backend import manager as manager_mod
    from backend.settings import Settings
    from backend.storage import Storage

    snapshot = {"reserved_mb": 1234, "peak_reserved_mb": 2048, "used_mem_mb": 3000, "total_mem_mb": 20480}

    def fake_gpu_memory(index=0):
        if gpu_mem_raises:
            raise RuntimeError("nvidia-smi not found")
        return dict(snapshot)

    orig_mem, orig_reset = device_mod.gpu_memory, device_mod.reset_peak_memory
    device_mod.gpu_memory = fake_gpu_memory
    device_mod.reset_peak_memory = lambda index=0: None
    try:
        settings = Settings()
        storage = Storage(settings)
        mgr = manager_mod.TaskManager(settings, storage, FakeGpuEngine())
        staged = tmp / "uploads" / "page.png"
        staged.parent.mkdir(parents=True, exist_ok=True)
        make_png(staged)
        meta = mgr.submit(
            input_path=staged,
            filename="page.png",
            size=staged.stat().st_size,
            params={},
            owner="test-session",
            pipeline="OCR",
            pipeline_label="纯文字识别（PP-OCRv6）",
        )
        task_id = meta["id"]
        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            cur = mgr.get(task_id) or {}
            status = cur.get("status")
            if status in ("success", "done", "failed", "canceled"):
                break
            time.sleep(0.1)
        log_file = storage.task_dir(task_id) / "task.log"
        log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        result = (status, cur.get("error") or "", log_text)
        mgr.shutdown()
        return result
    finally:
        device_mod.gpu_memory, device_mod.reset_peak_memory = orig_mem, orig_reset


def main() -> int:
    from backend import manager as manager_mod

    print("0) _page_mem_text 两个分支都必须有值")
    text_gpu = manager_mod._page_mem_text(True, 0)
    text_cpu = manager_mod._page_mem_text(False, 0)
    check("GPU 分支返回字符串", isinstance(text_gpu, str) and bool(text_gpu), repr(text_gpu))
    check("CPU 分支返回字符串", isinstance(text_cpu, str) and bool(text_cpu), repr(text_cpu))

    print("1) GPU 路径跑完整页（旧代码此处 UnboundLocalError）")
    with tempfile.TemporaryDirectory() as t:
        status, error, log_text = run_task(Path(t))
        check("任务完成", status in ("success", "done"), f"status={status} error={error}")
        check("错误信息为空", not error, error)
        check("日志里有显存信息", "显存池" in log_text, log_text[-400:])
        check("日志里有块数统计", "块/" in log_text or "块" in log_text, log_text[-400:])

    print("2) gpu_memory() 抛异常时任务不受影响")
    with tempfile.TemporaryDirectory() as t:
        status, error, log_text = run_task(Path(t), gpu_mem_raises=True)
        check("任务仍然完成", status in ("success", "done"), f"status={status} error={error}")
        check("日志降级为不可用", "不可用" in log_text or "显存池" in log_text, log_text[-300:])

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
