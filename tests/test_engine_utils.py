#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎结果归一化单元测试（不加载模型，纯函数级）。

覆盖踩过的坑：paddlex 3.7.x 的 `res.json` 外层多包了一层 {"res": {...}}，
不剥掉会导致"识别成功但结果为空"。

    python3 tests/test_engine_utils.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.engine import (  # noqa: E402
    blocks_from_ocr,
    blocks_from_structure,
    blocks_from_table,
    unwrap_result,
)

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  [通过] {name}")
    else:
        FAILED += 1
        print(f"  [失败] {name} {detail}")


OCR_PAYLOAD = {
    "input_path": "p1.jpg",
    "rec_texts": ["报价单", "合计：45450", "深圳市云图智能科技"],
    "rec_scores": [0.98, 0.95, 0.9],
    "rec_polys": [
        [[100, 100], [400, 100], [400, 160], [100, 160]],
        [[100, 180], [400, 180], [400, 240], [100, 240]],
        [[100, 300], [500, 300], [500, 360], [100, 360]],
    ],
    "dt_polys": [],
}


def main() -> int:
    print("引擎结果归一化测试")

    # 1) 三种外层包装
    check("扁平结构解包", unwrap_result(dict(OCR_PAYLOAD)).get("rec_texts") == OCR_PAYLOAD["rec_texts"])
    check("{'res': {...}} 解包", unwrap_result({"res": OCR_PAYLOAD}).get("rec_texts") == OCR_PAYLOAD["rec_texts"])
    check(
        "嵌套两层 {'result': {'res': {...}}}",
        unwrap_result({"result": {"res": OCR_PAYLOAD}}).get("rec_texts") == OCR_PAYLOAD["rec_texts"],
    )
    check("空字典不抛异常", unwrap_result({}) == {})
    check("非字典不抛异常", unwrap_result(None) == {})

    # 2) OCR 结果 -> blocks（应聚成 2 段：前两行 y 相邻；第三行间距大）
    blocks = blocks_from_ocr(unwrap_result({"res": OCR_PAYLOAD}))
    check("OCR 生成 block", len(blocks) >= 1, f"blocks={len(blocks)}")
    text = "\n".join(b.text for b in blocks)
    check("OCR 文本完整", "报价单" in text and "45450" in text, text[:80])
    check("OCR 段落聚类（前两行合并）", len(blocks) <= 2, f"blocks={len(blocks)}")

    # 3) PP-StructureV3 结果 -> blocks
    structure = {
        "res": {
            "width": 1654,
            "height": 2339,
            "parsing_res_list": [
                {"block_label": "doc_title", "block_content": "报价单", "block_bbox": [600, 100, 1000, 160], "block_order": 1},
                {"block_label": "text", "block_content": "客户名称：某某公司", "block_bbox": [120, 240, 900, 290], "block_order": 2},
                {
                    "block_label": "table",
                    "block_content": "<table><tr><th>序号</th><th>金额</th></tr><tr><td>1</td><td>39900</td></tr></table>",
                    "block_bbox": [120, 380, 1400, 700],
                    "block_order": 3,
                },
                {"block_label": "header", "block_content": "页眉水印", "block_bbox": [0, 0, 100, 40], "block_order": None},
                {"block_label": "image", "block_content": "", "block_bbox": [120, 800, 400, 1000], "block_order": None},
            ],
        }
    }
    blocks = blocks_from_structure(unwrap_result(structure))
    types = [b.type for b in blocks]
    check("结构解析出 5 个块", len(blocks) == 5, str(types))
    check("标题类型", types[0] == "title", str(types))
    check("表格块带结构", blocks[2].type == "table" and blocks[2].table is not None and blocks[2].table.n_rows == 2)
    texts = [c.text for c in blocks[2].table.cells]
    check("表格单元格内容", texts == ["序号", "金额", "1", "39900"], str(texts))
    check("页眉标记为噪声", blocks[3].is_noise, blocks[3].type)
    check("图片块占位", blocks[4].type == "figure" and "[图片]" in blocks[4].text, blocks[4].text)

    # 4) table 块内容是纯文本（format_block_content=False）时不应崩
    plain_table = {
        "parsing_res_list": [
            {"block_label": "table", "block_content": "序号 金额 1 39900", "block_bbox": [0, 0, 10, 10], "block_order": 1}
        ],
        "table_res_list": [
            {"pred_html": "<table><tr><td>序号</td><td>金额</td></tr><tr><td>1</td><td>39900</td></tr></table>"}
        ],
    }
    blocks = blocks_from_structure(unwrap_result(plain_table))
    check(
        "无 HTML 时用 table_res_list 兜底",
        blocks and blocks[0].table is not None and blocks[0].table.n_rows == 2,
        str(blocks[0].type if blocks else "empty"),
    )

    # --- 通用表格识别产线（table_recognition_v2）：只有 table_res_list ---
    def check_table_pipeline() -> None:
        payload = {
            "page_index": None,
            "layout_det_res": {
                "boxes": [
                    {"label": "text", "coordinate": [0, 0, 100, 50]},
                    {"label": "table", "coordinate": [10, 60, 300, 200]},
                ]
            },
            "overall_ocr_res": {"rec_texts": ["序号", "金额"], "rec_scores": [0.9, 0.9], "rec_polys": []},
            "table_res_list": [
                {
                    "cell_box_list": [
                        [[12, 62], [60, 62], [60, 90], [12, 90]],
                        [[62, 62], [150, 62], [150, 90], [62, 90]],
                        [[12, 92], [60, 92], [60, 120], [12, 120]],
                        [[62, 92], [150, 92], [150, 120], [62, 120]],
                    ],
                    "pred_html": (
                        "<html><body><table>"
                        "<tr><td>序号</td><td>金额</td></tr>"
                        "<tr><td>1</td><td>39900</td></tr>"
                        "</table></body></html>"
                    ),
                }
            ],
        }
        blocks = blocks_from_table(unwrap_result(payload))
        check("表格产线：解析出 1 个表格块", len(blocks) == 1 and blocks[0].type == "table", str(blocks))
        table = blocks[0].table if blocks else None
        check("表格产线：2x2 结构", table is not None and (table.n_rows, table.n_cols) == (2, 2), str(table))
        check(
            "表格产线：单元格文本正确",
            table is not None and [c.text for c in table.cells] == ["序号", "金额", "1", "39900"],
            str([c.text for c in table.cells] if table else None),
        )
        check(
            "表格产线：bbox 取单元格框并集",
            blocks and blocks[0].bbox == [12.0, 62.0, 150.0, 120.0],
            str(blocks[0].bbox if blocks else None),
        )

        # 没有表格时退回 OCR 文本，避免"识别成功但内容为空"
        no_table = {
            "overall_ocr_res": {
                "rec_texts": ["纯文字段落一", "纯文字段落二"],
                "rec_scores": [0.9, 0.9],
                "rec_polys": [
                    [[10, 10], [200, 10], [200, 40], [10, 40]],
                    [[10, 60], [200, 60], [200, 90], [10, 90]],
                ],
            },
            "table_res_list": [],
        }
        fallback = blocks_from_table(unwrap_result(no_table))
        check(
            "表格产线：无表格时退回 OCR 文本",
            len(fallback) >= 1 and all(b.type == "text" for b in fallback),
            str([(b.type, b.text[:8]) for b in fallback]),
        )

        # 表头 colspan 场景：HTML 合并单元格必须还原
        span_payload = {
            "table_res_list": [
                {
                    "pred_html": (
                        "<table><tr><td rowspan='2'>序号</td><td colspan='2'>产品信息</td></tr>"
                        "<tr><td>名称</td><td>型号</td></tr>"
                        "<tr><td>1</td><td>AI 一体机</td><td>Xpark</td></tr></table>"
                    )
                }
            ],
            "overall_ocr_res": {},
        }
        sb = blocks_from_table(unwrap_result(span_payload))
        st = sb[0].table if sb else None
        spans = sorted(
            (c.row, c.col, c.rowspan, c.colspan) for c in (st.cells if st else []) if c.rowspan > 1 or c.colspan > 1
        )
        check("表格产线：rowspan/colspan 还原", spans == [(0, 0, 2, 1), (0, 1, 1, 2)], str(spans))

    check_table_pipeline()

    print()
    if FAILED:
        print(f"失败 {FAILED} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
