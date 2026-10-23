#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表格解析单元测试（无第三方依赖，可直接用系统 python 运行）。

    python3 tests/test_tableparse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.tableparse import parse_markdown_table, parse_table_html  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name} {detail}")


def main() -> int:
    print("表格解析单元测试")

    # 1) 基本行列
    t = parse_table_html("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>")
    check("2x2 行列数", t.n_rows == 2 and t.n_cols == 2, f"{t.n_rows}x{t.n_cols}")
    check("2x2 单元格数", len(t.cells) == 4)

    # 2) rowspan / colspan
    html = """
    <table>
      <tr><th rowspan="2">序号</th><th colspan="2">金额</th><th rowspan="2">备注</th></tr>
      <tr><th>单价</th><th>数量</th></tr>
      <tr><td>1</td><td>10.5</td><td>2</td><td>含税</td></tr>
    </table>
    """
    t = parse_table_html(html)
    spans = {(c.row, c.col): (c.rowspan, c.colspan) for c in t.cells}
    check("3 行 4 列", t.n_rows == 3 and t.n_cols == 4, f"{t.n_rows}x{t.n_cols}")
    check("rowspan=2 记录正确", spans.get((0, 0)) == (2, 1), str(spans.get((0, 0))))
    check("colspan=2 记录正确", spans.get((0, 1)) == (1, 2), str(spans.get((0, 1))))
    check("第三行第 2 列定位正确", spans.get((2, 1)) == (1, 1), str(spans.get((2, 1))))
    grid = t.plain_rows()
    check("合并区域文本填充", grid[1][0] == "序号" and grid[1][1] == "单价", str(grid[1]))
    check("TSV 列数一致", all(len(row.split("\t")) == 4 for row in t.to_tsv().splitlines()))

    # 3) 缺闭合标签的容错
    t = parse_table_html("<table><tr><td>a<td>b<tr><td>c<td>d</table>")
    check("缺失 </td></tr> 容错", t.n_rows == 2 and t.n_cols == 2, f"{t.n_rows}x{t.n_cols}")

    # 4) <br> 与实体
    t = parse_table_html("<table><tr><td>第一行<br>第二行</td><td>a&amp;b</td></tr></table>")
    check("br 转换行", t.cells[0].text == "第一行\n第二行", repr(t.cells[0].text))
    check("HTML 实体反转义", t.cells[1].text == "a&b", repr(t.cells[1].text))

    # 5) 非表格内容
    t = parse_table_html("纯文本，不是表格")
    check("非表格返回空表", t.is_empty)

    # 6) Markdown 管道表
    md = """
| 姓名 | 部门 | 工号 |
| --- | --- | --- |
| 张三 | 研发部 | A001 |
| 李四 | 财务部 | B002 |
"""
    t = parse_markdown_table(md)
    check("MD 表 3 行 3 列", t.n_rows == 3 and t.n_cols == 3, f"{t.n_rows}x{t.n_cols}")
    check("MD 表内容", t.cells[4].text == "研发部", t.cells[4].text)

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
