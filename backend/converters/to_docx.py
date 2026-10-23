"""Word (.docx) 导出。

要点：
  * 标题/正文分级映射；中文字体设置 eastAsia 属性，避免出现方块字；
  * 表格使用真实 Word 表格 + 单元格合并（rowspan/colspan -> merge），保证结构正确；
  * 多页 PDF 每页之间插入分页符，尽量贴近原文档版式。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..docmodel import Block, Doc
from ..tableparse import Table

BODY_FONT_EAST = "宋体"
BODY_FONT_LATIN = "Times New Roman"
TABLE_FONT_EAST = "宋体"
TABLE_FONT_SIZE = 9


def _apply_style_font(style, latin: str, east: str, size=None, bold=None) -> None:  # noqa: ANN001
    try:
        font = style.font
        font.name = latin
        if size is not None:
            font.size = Pt(size)
        if bold is not None:
            font.bold = bold
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        rfonts.set(qn("w:ascii"), latin)
        rfonts.set(qn("w:hAnsi"), latin)
        rfonts.set(qn("w:eastAsia"), east)
    except Exception:  # noqa: BLE001 - 字体设置失败不应中断导出
        pass


def _set_run_font(run, size: float = TABLE_FONT_SIZE, bold: bool = False, east: str = TABLE_FONT_EAST) -> None:  # noqa: ANN001
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = BODY_FONT_LATIN
    rpr = run._element.get_or_add_rPr()  # noqa: SLF001
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), east)


def _write_cell(cell, text: str, bold: bool = False) -> None:  # noqa: ANN001
    cell.text = ""
    lines = (text or "").split("\n") or [""]
    paragraph = cell.paragraphs[0]
    for i, line in enumerate(lines):
        if i:
            paragraph = cell.add_paragraph()
        run = paragraph.add_run(line)
        _set_run_font(run, bold=bold)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.space_before = Pt(0)


def _repeat_header(table) -> None:  # noqa: ANN001
    """首行作为跨页重复的表头。"""
    try:
        tr = table.rows[0]._tr  # noqa: SLF001
        trpr = tr.get_or_add_trPr()
        header = OxmlElement("w:tblHeader")
        header.set(qn("w:val"), "true")
        trpr.append(header)
    except Exception:  # noqa: BLE001
        pass


def _add_table(document, table: Table) -> None:  # noqa: ANN001
    if table.is_empty:
        return
    word_table = document.add_table(rows=table.n_rows, cols=table.n_cols)
    word_table.style = "Table Grid"
    word_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    word_table.autofit = True

    # 1) 先做合并（必须从合并起点开始）
    for cell in table.cells:
        if cell.rowspan > 1 or cell.colspan > 1:
            r_end = min(cell.row + cell.rowspan, table.n_rows) - 1
            c_end = min(cell.col + cell.colspan, table.n_cols) - 1
            try:
                word_table.cell(cell.row, cell.col).merge(word_table.cell(r_end, c_end))
            except Exception:  # noqa: BLE001
                pass

    # 2) 再写内容（只写合并起点，避免重复）
    for cell in table.cells:
        target = word_table.cell(cell.row, cell.col)
        _write_cell(target, cell.text, bold=cell.is_header or cell.bold)
        target.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    if any(c.is_header for c in table.cells) or table.n_rows > 1:
        _repeat_header(word_table)

    document.add_paragraph()


def _add_block(document, block: Block) -> None:  # noqa: ANN001
    text = block.text or ""
    if block.type == "title":
        level = 1 if block.label in ("doc_title", "title") else 2
        heading = document.add_heading(level=level)
        run = heading.add_run(text)
        run.font.color.rgb = RGBColor(0, 0, 0)
        _set_run_font(run, size=16 if level == 1 else 13, bold=True, east=BODY_FONT_EAST)
        if level == 1:
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif block.type == "table" and block.table is not None:
        _add_table(document, block.table)
    elif block.type == "figure":
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"[图片] {text}".strip())
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        _set_run_font(run, size=10)
    elif block.type == "formula":
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.font.name = "Cambria Math"
        _set_run_font(run, size=11)
    elif block.type == "caption":
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        _set_run_font(run, size=9)
        run.font.italic = True
    else:
        for line in text.split("\n"):
            p = document.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.25
            run = p.add_run(line)
            _set_run_font(run, size=10.5, east=BODY_FONT_EAST)


def build_docx(doc: Doc, out_path: Path, include_noise: bool = False, page_break: bool = True) -> Path:
    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)

    _apply_style_font(document.styles["Normal"], BODY_FONT_LATIN, BODY_FONT_EAST, size=10.5)
    for name in ("Heading 1", "Heading 2", "Title"):
        if name in [s.name for s in document.styles]:
            _apply_style_font(document.styles[name], BODY_FONT_LATIN, BODY_FONT_EAST)
            try:
                document.styles[name].font.color.rgb = RGBColor(0, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    total_pages = len(doc.pages)
    for page in doc.pages:
        blocks: List[Block] = page.visible_blocks(include_noise=include_noise)
        for block in blocks:
            _add_block(document, block)
        if page_break and total_pages > 1 and page.index != total_pages:
            document.add_page_break()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out_path))
    return out_path
