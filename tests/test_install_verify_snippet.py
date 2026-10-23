#!/usr/bin/env python3
"""install_offline.sh 第 5/6 步（安装结果校验）的行为测试。

背景（真实踩过的坑）：
  这段校验原先写的是「先 for 循环 import 所有模块 → 只要有一个失败就 sys.exit(1)」，
  后面的 `PADDLEX_OCR_ALLOW_NO_DRIVER` 容错分支永远走不到——因为 import paddle
  在循环里就已经失败退出了。结果：在**没有显卡的打包机**上执行
  `install_offline.sh --device gpu` 必然判失败，而脚本却声称"无驱动可容忍"。

本测试把脚本里的那段 Python 原样抽出来，配合假模块跑三种场景：
  1. 无驱动（libcuda 缺失）+ ALLOW_NO_DRIVER=1  -> 退出 0，打 WARN，不判失败
  2. 无驱动 + 未开容错                        -> 退出 1（真错时要报错）
  3. 依赖齐全、paddle 可导入（CPU 编译）        -> 退出 0，打印 cuda=False

用法：python3 tests/test_install_verify_snippet.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install_offline.sh"

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


def extract_snippet() -> str:
    """取出 `if ! "$VENV_PY" - <<'PY' ... PY` 之间的 Python 源码。"""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S)
    assert m, "没找到 install_offline.sh 里的校验片段（heredoc 结构变了？）"
    return m.group(1)


def write_stubs(directory: Path, *, paddle_breaks: bool) -> None:
    """生成假模块：只用最小属性，避免依赖真实 paddle（它需要 CUDA 运行库）。"""
    simple = ("fastapi", "uvicorn", "yaml", "PIL", "docx", "openpyxl", "psutil", "paddlex")
    for name in simple:
        (directory / f"{name}.py").write_text('__version__ = "test"\n', encoding="utf-8")

    if paddle_breaks:
        (directory / "paddle.py").write_text(
            'raise OSError("libcuda.so.1: cannot open shared object file: '
            'No such file or directory")\n',
            encoding="utf-8",
        )
    else:
        (directory / "paddle.py").write_text(
            "__version__ = '3.4.0'\n"
            "class _Device:\n"
            "    @staticmethod\n"
            "    def is_compiled_with_cuda():\n"
            "        return False\n"
            "device = _Device()\n",
            encoding="utf-8",
        )


def run_snippet(snippet: str, stub_dir: Path, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PADDLEX_OCR_ALLOW_NO_DRIVER", None)
    env.update(env_extra)
    env["PYTHONPATH"] = str(stub_dir)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
        fh.write(snippet)
        path = fh.name
    try:
        return subprocess.run(
            [sys.executable, path], capture_output=True, text=True, env=env, timeout=120
        )
    finally:
        os.unlink(path)


def main() -> int:
    snippet = extract_snippet()

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        # 1) 无驱动 + 容错：应退出 0，且不能出现 FAIL paddle
        d1 = base / "no_driver_allow"
        d1.mkdir()
        write_stubs(d1, paddle_breaks=True)
        r = run_snippet(snippet, d1, {"PADDLEX_OCR_ALLOW_NO_DRIVER": "1"})
        check("无驱动+容错：退出码 0", r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        check("无驱动+容错：给出 WARN 与验收指引", "WARN" in r.stdout and "smoke_test" in r.stdout, r.stdout)
        check("无驱动+容错：不出现 FAIL paddle", "FAIL  paddle" not in r.stdout, r.stdout)

        # 2) 无驱动但未开容错：必须报错（真装坏时要能被发现）
        d2 = base / "no_driver_strict"
        d2.mkdir()
        write_stubs(d2, paddle_breaks=True)
        r = run_snippet(snippet, d2, {})
        check("无驱动+严格：退出码非 0", r.returncode != 0, f"rc={r.returncode}")
        check("无驱动+严格：标出 FAIL paddle", "FAIL  paddle" in r.stdout, r.stdout)

        # 3) 依赖齐全：退出 0 并打印 cuda 状态
        d3 = base / "ok"
        d3.mkdir()
        write_stubs(d3, paddle_breaks=False)
        r = run_snippet(snippet, d3, {})
        check("依赖齐全：退出码 0", r.returncode == 0, f"rc={r.returncode}\n{r.stdout}")
        check("依赖齐全：打印 cuda=False", "cuda=False" in r.stdout, r.stdout)
        check("依赖齐全：打印解释器", "解释器:" in r.stdout, r.stdout)

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
