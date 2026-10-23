"""Excel (.xlsx) 导出。

结构：
  * 首个工作表「全文」：按阅读顺序输出非表格文本（便于检索/校对）
  * 每个识别到的表格单独一个工作表（表1、表2 …）：真实单元格 + 合并单元格 + 边框

合并单元格映射：
  HTML <td rowspan=2 colspan=3> -> 起点单元格写值，其余被覆盖格不写值，
  再用 openpyxl 的 merge_cells 合并，数值/文本/单位原样保留（不做类型猜测，
  避免 "00123" 被 Excel 吃成 123、"-" 被当成公式）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..docmodel import Block, Doc
from ..tableparse import Table

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill("solid", fgColor="EDF2F7")
HEADER_FONT = Font(name="等线", bold=True, size=10)
BODY_FONT = Font(name="等线", size=10)
INVALID_SHEET_CHARS = re.compile(r"[\\/:*?\[\]]")


def _sheet_name(raw: str, used: set) -> str:
    name = INVALID_SHEET_CHARS.sub("_", (raw or "表").strip()) or "表"
    name = name[:31]
    candidate = name
    i = 2
    while candidate in used:
        suffix = f"_{i}"
        candidate = name[: 31 - len(suffix)] + suffix
        i += 1
    used.add(candidate)
    return candidate


def _looks_numeric(text: str) -> bool:
    if not text:
        return False
    t = text.replace(",", "").replace("￥", "").replace("¥", "").replace("$", "").strip()
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1]
    try:
        float(t)
        return True
    except ValueError:
        return False


def _cell_value(text: str):
    """纯数字且未被前导零污染时写为数字，其余一律写文本（保证不丢精度/前导零）。"""
    if not text:
        return None
    stripped = text.strip()
    if (
        _looks_numeric(stripped)
        and not re.match(r"^0\d", stripped)
        and len(stripped) <= 15
        and "\n" not in stripped
    ):
        try:
            value = float(stripped.replace(",", "").replace("￥", "").replace("¥", ""))
            return int(value) if value.is_integer() and abs(value) < 1e15 else value
        except ValueError:
            return text
    return text


def _write_text_sheet(ws, doc: Doc, include_noise: bool) -> None:  # noqa: ANN001
    ws.title = "全文"
    ws.column_dimensions["A"].width = 110
    row = 1
    ws.cell(row=row, column=1, value=f"{doc.name}（{doc.pipeline}）").font = Font(bold=True, size=12)
    row += 2
    for page in doc.pages:
        if len(doc.pages) > 1:
            ws.cell(row=row, column=1, value=f"—— 第 {page.index} 页 ——").font = Font(
                bold=True, color="666666"
            )
            row += 1
        for block in page.visible_blocks(include_noise=include_noise):
            if block.type == "table" and block.table is not None:
                ws.cell(
                    row=row, column=1,
                    value=f"[表格 {block.table.n_rows}行×{block.table.n_cols}列] 内容见"f"同名工作表",
                ).font = Font(name="等线", size=9, color="808080")
                row += 1
                continue
            text = (block.text or "").strip()
            if not text:
                continue
            cell = ws.cell(row=row, column=1, value=text)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.font = Font(name="等线", size=10, bold=block.type == "title")
            lines = max(text.count("\n") + 1, len(text) // 60 + 1)
            ws.row_dimensions[row].height = min(15 * lines, 300)
            row += 1
        row += 1


def _write_table_sheet(ws, table: Table) -> None:  # noqa: ANN001
    for r in range(table.n_rows):
        for c in range(table.n_cols):
            cell = ws.cell(row=r + 1, column=c + 1)
            cell.border = BORDER
            cell.font = BODY_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="left")

    for cell in table.cells:
        target = ws.cell(row=cell.row + 1, column=cell.col + 1)
        target.value = _cell_value(cell.text)
        if cell.is_header or cell.bold:
            target.font = HEADER_FONT
            target.fill = HEADER_FILL
            target.alignment = Alignment(
                wrap_text=True, vertical="center", horizontal="center"
            )

    for cell in table.cells:
        if cell.rowspan > 1 or cell.colspan > 1:
            r1, r2 = cell.row + 1, min(cell.row + cell.rowspan, table.n_rows)
            c1, c2 = cell.col + 1, min(cell.col + cell.colspan, table.n_cols)
            try:
                ws.merge_cells(
                    start_row=r1, start_column=c1, end_row=r2, end_column=c2
                )
                ws.cell(row=r1, column=c1).alignment = Alignment(
                    wrap_text=True, vertical="center", horizontal="center"
                )
            except Exception:  # noqa: BLE001 - 合并冲突时保留内容优先
                pass

    # 依据内容估算列宽
    widths = [6] * table.n_cols
    for cell in table.cells:
        text = cell.text or ""
        longest = max((len(line) for line in text.split("\n")), default=0)
        span = max(cell.colspan, 1)
        per_col = max(int(longest / span) + 3, 6)
        for c in range(cell.col, min(cell.col + cell.colspan, table.n_cols)):
            widths[c] = max(widths[c], per_col)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(width, 48)

    if table.n_rows:
        ws.freeze_panes = "A2"


def build_xlsx(doc: Doc, out_path: Path, include_noise: bool = False) -> Path:
    wb = Workbook()
    used_names: set = set()
    ws_main = wb.active
    used_names.add("全文")
    _write_text_sheet(ws_main, doc, include_noise)

    index = 0
    for page in doc.pages:
        for block in page.blocks:
            if block.type != "table" or block.table is None or block.table.is_empty:
                continue
            index += 1
            title = f"表{index}"
            if block.text and not block.text.lstrip().startswith("<") and "\t" not in block.text:
                caption = block.text.strip().splitlines()[0][:20]
                if caption:
                    title = f"表{index}-{caption}"
            ws = wb.create_sheet(_sheet_name(title, used_names))
            _write_table_sheet(ws, block.table)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return out_path
