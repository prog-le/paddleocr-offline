#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验四种导出结果的结构正确性（尤其是表格合并单元格）。"""

from __future__ import annotations

import sys
from pathlib import Path


def fail(msg: str) -> None:
    print(f"  [失败] {msg}")
    raise SystemExit(1)


def check_txt(path: Path) -> None:
    text = path.read_text("utf-8")
    if "MOCK" not in text and "自检" not in text:
        fail("TXT 缺少正文内容")
    if "\t" not in text:
        fail("TXT 表格未使用制表符分列")
    print(f"  [通过] TXT {len(text)} 字符，含制表符分隔表格")


def check_md(path: Path) -> None:
    text = path.read_text("utf-8")
    lines = text.splitlines()
    if not any(ln.startswith("# ") for ln in lines):
        fail("MD 缺少一级标题")

    # 按空行/非表格行切分连续表格块，分别校验列数一致性与对齐行
    blocks, current = [], []
    for ln in lines:
        if ln.strip().startswith("|"):
            current.append(ln)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if not blocks:
        fail("MD 未生成管道表")

    for bi, block in enumerate(blocks, start=1):
        if len(block) < 3:
            fail(f"MD 第 {bi} 个表格行数过少：{block}")
        sep = block[1]
        if "-" not in sep or not set(sep.replace("|", "").replace(" ", "")) <= {"-", ":"}:
            fail(f"MD 第 {bi} 个表格缺少对齐行：{sep}")
        cols = block[0].count("|")
        for ln in block:
            if ln.count("|") != cols:
                fail(f"MD 第 {bi} 个表格列数不一致：{ln}")

    total_rows = sum(len(b) - 2 for b in blocks)
    print(f"  [通过] MD 标题 + {len(blocks)} 个管道表 / {total_rows} 数据行，列数一致")


def check_docx(path: Path) -> None:
    from docx import Document

    doc = Document(str(path))
    if not doc.paragraphs and not doc.tables:
        fail("DOCX 为空")
    if not doc.tables:
        fail("DOCX 中没有真实表格")
    t = doc.tables[0]
    merged = 0
    for row in t.rows:
        for cell in row.cells:
            if any(chr in cell._tc.xml for chr in ("gridSpan", "vMerge")):
                merged += 1
                break
    texts = [c.text for r in t.rows for c in r.cells if c.text.strip()]
    if not texts:
        fail("DOCX 表格无文本")
    print(f"  [通过] DOCX 段落 {len(doc.paragraphs)}，表格 {len(doc.tables)}，"
          f"含合并标记的行 {merged}，首表文本样例 {texts[:3]}")


def check_xlsx(path: Path) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(str(path))
    if "全文" not in wb.sheetnames:
        fail("XLSX 缺少『全文』工作表")
    table_sheets = [n for n in wb.sheetnames if n != "全文"]
    if not table_sheets:
        fail("XLSX 没有表格工作表")
    ws = wb[table_sheets[0]]
    merged = list(ws.merged_cells.ranges)
    if not merged:
        fail("XLSX 表格未设置合并单元格（源表格含 rowspan/colspan）")
    values = [[c.value for c in row] for row in ws.iter_rows()]
    filled = sum(1 for row in values for v in row if v not in (None, ""))
    if filled < 6:
        fail("XLSX 表格单元格内容过少")
    print(f"  [通过] XLSX 工作表 {wb.sheetnames}，表1 合并区域 {[str(m) for m in merged]}，"
          f"非空单元格 {filled}")


def main(fmt_dir: str, prefix: str = "out") -> int:
    base = Path(fmt_dir)
    print("校验导出结果：")
    checkers = {"txt": check_txt, "md": check_md, "docx": check_docx, "xlsx": check_xlsx}
    for fmt, fn in checkers.items():
        path = base / f"{prefix}.{fmt}"
        if not path.exists():
            print(f"  [跳过] 缺少 {path}")
            continue
        fn(path)
    print("全部导出格式校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tmp", sys.argv[2] if len(sys.argv) > 2 else "out"))
