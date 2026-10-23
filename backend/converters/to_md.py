"""Markdown 导出。

映射规则：
  * title/doc_title  -> "# 标题"；paragraph_title -> "## 标题"
  * text             -> 普通段落（块内换行保留为行末两个空格 + 换行）
  * table            -> GFM 管道表（合并单元格：内容保留在左上角起点，被覆盖格留空，列数对齐）
  * figure           -> "> [图片]"（本工具不导出原图，仅占位保证结构完整）
  * formula          -> "$$ 公式 $$"
"""

from __future__ import annotations

from typing import List

from ..docmodel import Block, Doc
from ..tableparse import Table


def _escape_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", "<br>").strip()


def table_to_markdown(table: Table) -> str:
    if table.is_empty:
        return ""
    n_cols = table.n_cols
    n_rows = table.n_rows

    # 起点格 -> 文本；被合并覆盖的格子留空，保证列数一致
    origin = {(c.row, c.col): c for c in table.cells}
    lines: List[str] = []
    for r in range(n_rows):
        row_values = []
        for c in range(n_cols):
            cell = origin.get((r, c))
            row_values.append(_escape_cell(cell.text) if cell is not None else "")
        lines.append("| " + " | ".join(row_values) + " |")
        if r == 0:
            lines.append("| " + " | ".join("---" for _ in range(n_cols)) + " |")
    return "\n".join(lines)


def build_markdown(doc: Doc, include_noise: bool = False) -> str:
    out: List[str] = []
    has_title = any(b.type == "title" for p in doc.pages for b in p.visible_blocks(include_noise))
    if not has_title and doc.name:
        out.append(f"# {doc.name}")
        out.append("")

    for page in doc.pages:
        if len(doc.pages) > 1:
            out.append(f"<!-- 第 {page.index} 页 -->")
            out.append("")
        for block in page.visible_blocks(include_noise=include_noise):
            text = (block.text or "").strip()
            if block.type == "title":
                level = "#" if block.label in ("doc_title", "title", "doc_title_") else "##"
                if text:
                    out.append(f"{level} {text}")
                    out.append("")
            elif block.type == "table" and block.table is not None:
                md = table_to_markdown(block.table)
                if md:
                    out.append(md)
                    out.append("")
                elif text:
                    out.append(text)
                    out.append("")
            elif block.type == "formula":
                if text:
                    out.append(f"$$\n{text}\n$$")
                    out.append("")
            elif block.type == "figure":
                out.append(f"> [图片] {text}".rstrip())
                out.append("")
            elif block.type == "caption":
                if text:
                    out.append(f"*{text}*")
                    out.append("")
            else:
                if text:
                    out.append(text.replace("\n", "  \n"))
                    out.append("")
    return "\n".join(out).strip() + "\n"


def block_to_markdown(block: Block, include_noise: bool = False) -> str:
    """单块渲染（供预览使用）。"""
    if block.is_noise and not include_noise:
        return ""
    if block.type == "table" and block.table is not None:
        return table_to_markdown(block.table)
    return block.text or ""
