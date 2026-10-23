"""TXT 导出：保留阅读顺序与原行结构，表格转为制表符分隔的二维文本。"""

from __future__ import annotations

from typing import List

from ..docmodel import Doc


def build_txt(doc: Doc, include_noise: bool = False, page_markers: bool = True) -> str:
    out: List[str] = []
    for page in doc.pages:
        if page_markers:
            if out:
                out.append("")
            out.append(f"===== 第 {page.index} 页 =====")
        for block in page.visible_blocks(include_noise=include_noise):
            if block.type == "table" and block.table is not None and not block.table.is_empty:
                out.append(block.table.to_tsv())
                out.append("")
            elif block.text.strip():
                prefix = ""
                if block.type == "title":
                    prefix = ""
                elif block.type == "figure":
                    prefix = "[图片] "
                out.append(prefix + block.text.rstrip())
                out.append("")
    text = "\n".join(out).strip() + "\n"
    return text
