"""HTML 表格解析：把 PaddleX 表格识别产线输出的 HTML 还原为二维网格。

为什么要自己解析：
  PaddleX 的表格结果以 HTML 字符串给出（<td rowspan=2 colspan=3>...），
  要"正确映射到 Excel 单元格 / Word 表格 / Markdown 表格"，
  必须先还原成带合并信息的网格，而不是简单按行切分。

支持的写法：
  * <td>/<th>，rowspan / colspan 属性（含单引号、无引号）
  * 单元格内 <br>（换行）、<p>（分段）、<b>/<strong>（加粗）
  * 缺失 </td>、</tr> 的容错（浏览器式自动闭合）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple


@dataclass
class Cell:
    text: str = ""
    row: int = 0
    col: int = 0
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False
    bold: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "text": self.text,
            "row": self.row,
            "col": self.col,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
            "is_header": self.is_header,
            "bold": self.bold,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "Cell":
        return cls(
            text=str(d.get("text") or ""),
            row=int(d.get("row") or 0),
            col=int(d.get("col") or 0),
            rowspan=max(int(d.get("rowspan") or 1), 1),
            colspan=max(int(d.get("colspan") or 1), 1),
            is_header=bool(d.get("is_header")),
            bold=bool(d.get("bold")),
        )


@dataclass
class Table:
    html: str = ""
    cells: List[Cell] = field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.cells or self.n_rows == 0 or self.n_cols == 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "html": self.html,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "cells": [c.to_dict() for c in self.cells],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "Table":
        return cls(
            html=str(d.get("html") or ""),
            cells=[Cell.from_dict(c) for c in (d.get("cells") or []) if isinstance(c, dict)],
            n_rows=int(d.get("n_rows") or 0),
            n_cols=int(d.get("n_cols") or 0),
        )

    # ------------------------------------------------------------------ grid
    def grid(self) -> List[List[Optional[Cell]]]:
        """返回 n_rows x n_cols 的网格，被合并覆盖的位置为 None。"""
        g: List[List[Optional[Cell]]] = [[None] * self.n_cols for _ in range(self.n_rows)]
        for cell in self.cells:
            for r in range(cell.row, min(cell.row + cell.rowspan, self.n_rows)):
                for c in range(cell.col, min(cell.col + cell.colspan, self.n_cols)):
                    if g[r][c] is None:
                        g[r][c] = cell if (r == cell.row and c == cell.col) else None
        return g

    def covered(self) -> Dict[Tuple[int, int], Cell]:
        """(row,col) -> 覆盖该位置的合并起点单元格（用于填充被合并格）。"""
        out: Dict[Tuple[int, int], Cell] = {}
        for cell in self.cells:
            for r in range(cell.row, min(cell.row + cell.rowspan, self.n_rows)):
                for c in range(cell.col, min(cell.col + cell.colspan, self.n_cols)):
                    out[(r, c)] = cell
        return out

    def plain_rows(self) -> List[List[str]]:
        """把合并单元格内容复制到每个被覆盖位置，得到规整的二维文本（用于 Excel/TXT 预览）。"""
        rows = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for (r, c), cell in self.covered().items():
            rows[r][c] = cell.text
        return rows

    def to_tsv(self) -> str:
        return "\n".join("\t".join(cell.replace("\n", " ") for cell in row) for row in self.plain_rows())


class _TableHTMLParser(HTMLParser):
    """只关心表格结构的最小解析器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Dict[str, object]]] = []
        self._row: Optional[List[Dict[str, object]]] = None
        self._cell: Optional[Dict[str, object]] = None
        self._buf: List[str] = []
        self._bold_depth = 0
        self._table_depth = 0

    # ------------------------------------------------------------- handlers
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            return
        if self._table_depth == 0:
            return
        if tag == "tr":
            self._close_cell()
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = {
                "text": "",
                "rowspan": _int_attr(attrs, "rowspan", 1),
                "colspan": _int_attr(attrs, "colspan", 1),
                "is_header": tag == "th",
                "bold": tag == "th",
            }
            self._buf = []
        elif tag in ("b", "strong"):
            self._bold_depth += 1
        elif tag == "br":
            self._buf.append("\n")
        elif tag in ("p", "div"):
            if self._buf and not self._buf[-1].endswith("\n"):
                self._buf.append("\n")
        elif tag in ("img",):
            # 表格里的图片没有文本可提取，占位符保留结构
            self._buf.append("[图]")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "table":
            self._close_cell()
            self._close_row()
            self._table_depth = max(self._table_depth - 1, 0)
            return
        if self._table_depth == 0:
            return
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._close_row()
        elif tag in ("b", "strong"):
            self._bold_depth = max(self._bold_depth - 1, 0)

    def handle_data(self, data: str) -> None:
        if self._cell is not None and self._table_depth > 0:
            self._buf.append(data)
        elif data.strip() and self._cell is None and self._row is not None:
            # <td> 外的裸文本（非规范 HTML），忽略
            pass

    # --------------------------------------------------------------- helpers
    def _close_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is None:
            self._row = []
        text = _clean_text("".join(self._buf))
        self._cell["text"] = text
        self._cell["bold"] = bool(self._cell.get("bold")) or self._bold_depth > 0
        self._row.append(self._cell)
        self._cell = None
        self._buf = []
        self._bold_depth = 0

    def _close_row(self) -> None:
        if self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def finish(self) -> List[List[Dict[str, object]]]:
        self._close_cell()
        self._close_row()
        return self.rows


def _int_attr(attrs, name: str, default: int) -> int:
    for key, value in attrs:
        if key.lower() == name and value is not None:
            try:
                return max(int(str(value).strip().strip("'\"")), 1)
            except ValueError:
                return default
    return default


def _clean_text(text: str) -> str:
    text = unescape(text or "")
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    # 去掉行首尾空白但保留内部换行
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def parse_table_html(html: str) -> Table:
    """解析 HTML 表格为 Table；解析失败返回空表（不抛异常）。"""
    if not html or "<t" not in html.lower():
        return Table(html=html or "")
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        rows = parser.finish()
    except Exception:  # noqa: BLE001 - 容错优先，坏 HTML 不应让整份文档失败
        return Table(html=html)
    if not rows:
        return Table(html=html)

    # 依据 rowspan/colspan 计算真实行列位置
    occupied: Dict[Tuple[int, int], int] = {}
    cells: List[Cell] = []
    max_col = 0
    for r, row in enumerate(rows):
        c = 0
        for raw in row:
            while (r, c) in occupied:
                c += 1
            rowspan = int(raw.get("rowspan") or 1)
            colspan = int(raw.get("colspan") or 1)
            cell = Cell(
                text=str(raw.get("text") or ""),
                row=r,
                col=c,
                rowspan=rowspan,
                colspan=colspan,
                is_header=bool(raw.get("is_header")),
                bold=bool(raw.get("bold")),
            )
            cells.append(cell)
            for rr in range(r, r + rowspan):
                for cc in range(c, c + colspan):
                    occupied[(rr, cc)] = 1
            c += colspan
            max_col = max(max_col, c)
    n_rows = max((c.row + c.rowspan for c in cells), default=0)
    table = Table(html=html, cells=cells, n_rows=n_rows, n_cols=max(max_col, 1))
    return table


def parse_markdown_table(text: str) -> Table:
    """解析 GFM 管道表（PP-StructureV3 在 format_block_content=True 时可能返回）。"""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    pipe_rows = [ln for ln in lines if ln.startswith("|")]
    if len(pipe_rows) < 2:
        return Table(html="")
    matrix: List[List[str]] = []
    for i, line in enumerate(pipe_rows):
        inner = line.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        parts = [p.strip() for p in inner.split("|")]
        if i == 1 and all(re.fullmatch(r":?-{2,}:?", p or "-") for p in parts):
            continue  # 对齐行
        matrix.append(parts)
    if not matrix:
        return Table(html="")
    n_cols = max(len(r) for r in matrix)
    cells: List[Cell] = []
    for r, row in enumerate(matrix):
        for c in range(n_cols):
            value = row[c] if c < len(row) else ""
            cells.append(Cell(text=value.replace("<br>", "\n"), row=r, col=c))
    return Table(html=text, cells=cells, n_rows=len(matrix), n_cols=n_cols)


def looks_like_html_table(text: str) -> bool:
    if not text:
        return False
    low = text.lstrip()[:200].lower()
    return "<table" in low or ("<tr" in low and "<td" in low)
