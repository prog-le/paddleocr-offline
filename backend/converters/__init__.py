"""导出统一入口。

    from backend.converters import export_document
    path = export_document(doc, "docx", out_dir / "name.docx", cfg)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from ..docmodel import Doc
from .to_docx import build_docx
from .to_md import build_markdown
from .to_txt import build_txt
from .to_xlsx import build_xlsx

FORMATS: Dict[str, Dict[str, str]] = {
    "docx": {
        "ext": ".docx",
        "media": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "label": "Word",
    },
    "xlsx": {
        "ext": ".xlsx",
        "media": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "label": "Excel",
    },
    "txt": {"ext": ".txt", "media": "text/plain; charset=utf-8", "label": "纯文本"},
    "md": {"ext": ".md", "media": "text/markdown; charset=utf-8", "label": "Markdown"},
}


class ExportError(RuntimeError):
    pass


def export_document(
    doc: Doc,
    fmt: str,
    out_path: Path,
    include_noise: bool = False,
    txt_page_markers: bool = True,
    docx_page_break: bool = True,
) -> Path:
    fmt = (fmt or "").lower().lstrip(".")
    if fmt not in FORMATS:
        raise ExportError(f"不支持的导出格式：{fmt}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "txt":
        text = build_txt(doc, include_noise=include_noise, page_markers=txt_page_markers)
        out_path.write_text(text, encoding="utf-8")
    elif fmt == "md":
        out_path.write_text(build_markdown(doc, include_noise=include_noise), encoding="utf-8")
    elif fmt == "docx":
        build_docx(doc, out_path, include_noise=include_noise, page_break=docx_page_break)
    elif fmt == "xlsx":
        build_xlsx(doc, out_path, include_noise=include_noise)
    return out_path


__all__ = ["FORMATS", "ExportError", "export_document"]
