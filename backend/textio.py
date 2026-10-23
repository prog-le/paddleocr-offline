"""配置文件读取的容错工具。

为什么需要（线上真实故障，2026-10 目标机启动即崩）：

    File "backend/main.py", line 77, in create_app
        pipeline_specs = build_registry(settings)
    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa3 in position 45

`config/pipelines/` 里混入了一个**非 UTF-8 文件**，两种常见来源：

  * macOS 拷贝产生的 `._OCR.yaml`（AppleDouble，二进制元数据）；
  * 被编辑器/终端存成 GBK 的 yaml（`0xa3` 是 GBK 里常见的首字节）。

旧实现用 `open(..., encoding="utf-8")` 直读，异常发生在 `import backend.main`
阶段，uvicorn 打印 traceback 后退出，用户看到的就是"服务起不来"。

本模块统一入口：
  1. 明显是隐藏文件/元数据（`.` 开头）、或含 NUL 字节的二进制 -> 返回 None（记日志）；
  2. 文本：UTF-8（含 BOM）优先 -> GB18030 兜底 -> 最后用替换字符硬解，永不抛异常。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TEXT_ENCODINGS = ("utf-8-sig", "gb18030")
APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def is_metadata_name(name: str) -> bool:
    """`.` 开头的隐藏文件、`.DS_Store`、编辑器备份等，都不是配置。"""
    return name.startswith(".") or name.lower().endswith((".ds_store", ".swp"))


def looks_binary(raw: bytes) -> bool:
    head = raw[:2048]
    return head.startswith(APPLEDOUBLE_MAGIC) or b"\x00" in head


def decode_text(raw: bytes) -> str:
    """UTF-8（含 BOM）优先，GB18030 兜底，最后替换字符硬解。"""
    for enc in TEXT_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_text_file(path: Path, *, what: str = "配置文件") -> Optional[str]:
    """读取文本文件；不可读/非文本返回 None（记日志，**绝不抛异常**）。"""
    if is_metadata_name(path.name):
        log.warning("跳过隐藏/元数据文件（不是%s）：%s", what, path.name)
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("跳过无法读取的%s %s：%s", what, path.name, exc)
        return None
    if looks_binary(raw):
        log.warning("跳过二进制文件（疑似 macOS 的 ._* 元数据或损坏文件）：%s", path.name)
        return None
    return decode_text(raw)
