"""Mock 推理引擎（PADDLEX_OCR_MOCK=1 启用）。

用途：在没有模型/没有 PaddleX 的环境里验证「上传 -> 队列 -> 识别 -> 预览 -> 导出」
整条链路，也可作为客户现场的连通性自检。真实部署请勿开启。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from .docmodel import Block
from .engine import PageResult
from .settings import Settings
from .tableparse import parse_table_html

MOCK_TABLE_HTML = """
<table>
  <tr><th rowspan="2">序号</th><th colspan="2">识别结果</th><th rowspan="2">备注</th></tr>
  <tr><th>字段</th><th>值</th></tr>
  <tr><td>1</td><td>发票号码</td><td>24312000000012345678</td><td>校验通过</td></tr>
  <tr><td>2</td><td>开票日期</td><td>2026年10月22日</td><td></td></tr>
  <tr><td>3</td><td>价税合计</td><td>¥ 1,280.00</td><td>含税</td></tr>
</table>
"""

MOCK_SIMPLE_TABLE_HTML = """
<table>
  <tr><th>项目</th><th>数量</th><th>单价</th></tr>
  <tr><td>打印纸 A4</td><td>10</td><td>25.00</td></tr>
  <tr><td>硒鼓</td><td>2</td><td>380.00</td></tr>
</table>
"""


class MockEngine:
    is_mock = True

    def __init__(self, settings: Settings, spec=None):
        self.settings = settings
        self.spec = spec
        self.pipeline_id = getattr(spec, "id", "MOCK")
        self.pipeline_name = self.pipeline_id
        self.label = getattr(spec, "label", "MOCK 自检")
        self.kind = getattr(spec, "kind", "structure")
        self.device = "cpu"
        self._predict_count = 0

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        return {
            "state": "ready",
            "pipeline": self.pipeline_id,
            "label": self.label,
            "device": "cpu",
            "loaded": True,
            "instances": 1,
            "error": None,
            "loaded_at": time.time(),
            "load_seconds": 0.0,
            "predict_count": self._predict_count,
            "last_predict_ms": 0,
            "mock": True,
        }

    def ensure_loaded(self) -> None:
        return None

    def model_inventory(self) -> Dict[str, Any]:
        return {"dir": str(self.settings.official_models_dir), "exists": False, "models": [], "mock": True}

    def close(self) -> None:
        return None

    # ------------------------------------------------------------------ 推理
    def predict_page(self, image_path: Path, params: Dict[str, Any]) -> PageResult:
        self._predict_count += 1
        started = time.time()
        page_no = 1
        stem = image_path.stem
        if "p" in stem:
            digits = "".join(ch for ch in stem if ch.isdigit())
            if digits:
                page_no = int(digits)
        time.sleep(0.05)

        blocks = [
            Block(type="title", label="doc_title", bbox=[80, 60, 900, 110],
                  text="离线 OCR / 表格识别自检文档（MOCK 模式）", order=1),
            Block(type="text", label="text", bbox=[80, 130, 900, 260],
                  text=(
                      f"这是第 {page_no} 页的模拟识别结果，用于验证任务队列、进度回报、"
                      "结果预览与四种格式导出链路是否正常。\n"
                      "真实识别由 PaddleX PP-StructureV3 产线完成，包含版面分析、"
                      "文本检测识别与表格结构还原。"
                  ),
                  order=2),
            Block(type="table", label="table", bbox=[80, 280, 900, 520],
                  text="发票信息表", table=parse_table_html(MOCK_TABLE_HTML), order=3),
            Block(type="caption", label="table_title", bbox=[80, 530, 900, 560],
                  text="表 1 含合并单元格的表格", order=4),
            Block(type="table", label="table", bbox=[80, 570, 900, 720],
                  text="采购明细", table=parse_table_html(MOCK_SIMPLE_TABLE_HTML), order=5),
            Block(type="text", label="text", bbox=[80, 740, 900, 800],
                  text="以上为模拟数据。MOCK 模式下不会加载任何模型，也不会联网。", order=6),
        ]
        return PageResult(
            blocks=blocks,
            width=1000,
            height=1400,
            raw={"mock": True, "page": page_no, "params": params},
            elapsed_ms=int((time.time() - started) * 1000),
        )

    def predict_pdf(self, pdf_path: Path, params: Dict[str, Any]) -> PageResult:
        """PDF 快抽产线的模拟实现（与 pdf_inspector 的产物形态一致：整份文档一块内容）。"""
        self._predict_count += 1
        started = time.time()
        time.sleep(0.05)

        blocks = [
            Block(type="title", label="doc_title", bbox=[0, 0, 0, 0],
                  text="离线 OCR / PDF 快抽自检文档（MOCK 模式）", order=1),
            Block(type="text", label="text", bbox=[0, 0, 0, 0],
                  text=(
                      f"这是 {pdf_path.name} 的模拟抽取结果，用于验证 PDF 产线的"
                      "任务队列、结果预览与四种格式导出链路是否正常。"
                  ),
                  order=2),
        ]
        return PageResult(
            blocks=blocks,
            width=0,
            height=0,
            raw={"mock": True, "pdf": pdf_path.name, "params": params},
            elapsed_ms=int((time.time() - started) * 1000),
        )
