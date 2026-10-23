#!/usr/bin/env python3
"""产线配置读取的容错单测（不需要模型、不需要网络）。

线上真实故障（目标机启动即崩）：
    File "backend/main.py", line 77, in create_app
        pipeline_specs = build_registry(settings)
    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa3 in position 45

`config/pipelines/` 下混入了一个**非 UTF-8 文本**（两种常见来源：
macOS 拷贝产生的 `._OCR.yaml` AppleDouble 二进制；被编辑器存成 GBK 的 yaml）。
旧实现用 `open(..., encoding="utf-8")` 直接读，异常冒到 import 阶段，整个服务起不来。

本测试固定这些行为：
  1. 正常 UTF-8 配置：label/kind 正确；
  2. GBK 保存的配置：能正确解码（中文不乱码），不抛异常；
  3. `._OCR.yaml`（AppleDouble 二进制）：跳过，不影响其它产线；
  4. 被截断的 UTF-8（半个汉字）：不崩，降级为替换字符；
  5. 隐藏文件 / 空文件：跳过或容忍。

用法：python3 tests/test_pipelines_encoding.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import pipelines as P  # noqa: E402

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name}  {detail}")


class FakeSettings:
    """build_registry 只需要 config_path 与 get()。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)

    def get(self, key: str, default=None):
        return default


UTF8_YAML = """# label: 纯文字识别（PP-OCRv6）
# description: PP-OCRv6 检测 + 识别，最快
# kind: ocr
pipeline_name: OCR
pipeline: {}
"""

APPLEDOUBLE = (
    b"\x00\x05\x16\x07"          # magic
    b"\x00\x02\x00\x00"          # version
    + b"\x00" * 16               # filler
    + b"\x00\x02"                # entry count
    + b"\x00\x00\x00\x02\x00\x00\x00\x30\x00\x00\x00\xa3"   # 资源分叉条目（含 0xa3）
    + b"\x00\x00\x00\x09\x00\x00\x00\xd3\x00\x00\x00\x20"
    + b"\xa3\xa3\xa3\xa3"
)


def build(tmp: Path, files: dict) -> list:
    pdir = tmp / "config" / "pipelines"
    pdir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        path = pdir / name
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
    return P.build_registry(FakeSettings(tmp / "config" / "config.yaml"))


def main() -> int:
    print("1) 正常 UTF-8 配置")
    with tempfile.TemporaryDirectory() as t:
        specs = build(Path(t), {"OCR.yaml": UTF8_YAML})
        by_id = {s.id: s for s in specs}
        check("解析出 OCR 产线", "OCR" in by_id, str(list(by_id)))
        check("label 正确", by_id["OCR"].label == "纯文字识别（PP-OCRv6）", by_id["OCR"].label)
        check("kind=ocr", by_id["OCR"].kind == "ocr", by_id["OCR"].kind)

    print("2) GBK 保存的配置（中文不乱码、不抛异常）")
    with tempfile.TemporaryDirectory() as t:
        gbk = UTF8_YAML.encode("gbk")
        check("GBK 样本确实不是合法 UTF-8", _is_bad_utf8(gbk))
        specs = build(Path(t), {"OCR.yaml": gbk})
        by_id = {s.id: s for s in specs}
        check("仍然解析出 OCR 产线", "OCR" in by_id, str(list(by_id)))
        check(
            "GBK 中文正确解码",
            by_id.get("OCR") and by_id["OCR"].label == "纯文字识别（PP-OCRv6）",
            by_id.get("OCR") and by_id["OCR"].label,
        )

    print("3) macOS 元数据文件 ._OCR.yaml（二进制）")
    with tempfile.TemporaryDirectory() as t:
        specs = build(Path(t), {"OCR.yaml": UTF8_YAML, "._OCR.yaml": APPLEDOUBLE})
        ids = [s.id for s in specs]
        check("不因二进制文件崩溃", True)
        check("跳过 ._OCR.yaml（不产生重复项）", ids.count("OCR") == 1, str(ids))
        check("正常产线仍在", "OCR" in ids, str(ids))

    print("4) 被截断的 UTF-8（半个汉字）")
    with tempfile.TemporaryDirectory() as t:
        broken = UTF8_YAML.encode("utf-8")[:20]      # 切在多字节字符中间
        check("样本不是合法 UTF-8", _is_bad_utf8(broken))
        specs = build(Path(t), {"OCR.yaml": broken})
        check("不崩溃且有兜底 OCR 产线", any(s.id == "OCR" for s in specs), str([s.id for s in specs]))

    print("5) 隐藏文件与空文件")
    with tempfile.TemporaryDirectory() as t:
        specs = build(
            Path(t),
            {"OCR.yaml": UTF8_YAML, ".hidden.yaml": "# label: 不该出现\n", "empty.yaml": ""},
        )
        ids = [s.id for s in specs]
        check("隐藏文件不注册", all(not i.startswith(".") for i in ids), str(ids))
        check("空文件不崩", True)

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


def _is_bad_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
