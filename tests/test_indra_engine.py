"""Indra OCR 适配层单测（不依赖 indra-ocr 本体）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import pipelines as P  # noqa: E402
from backend.indra_engine import blocks_from_indra_page  # noqa: E402
from backend.tableparse import Table  # noqa: E402


class FakePage:
    def __init__(self, blocks=None, text="", markdown="", width=0, height=0, lines=None):
        self.blocks = blocks or []
        self.text = text
        self.markdown = markdown
        self.width = width
        self.height = height
        self.lines = lines or []
        self.timings = {"total_ms": 12}


class FakeSettings:
    def __init__(self, values=None):
        self.values = values or {}
        self.config_path = PROJECT_ROOT / "config" / "config.yaml"

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


def test_blocks_map_labels_and_tables():
    html = "<table><tr><th>姓名</th><th>年龄</th></tr><tr><td>张三</td><td>18</td></tr></table>"
    page = FakePage(
        blocks=[
            {"label": "Section-Header", "bbox": [10, 5, 300, 40], "text": "年度报告"},
            {"label": "Text", "bbox": [10, 50, 300, 200], "text": "正文第一段"},
            {"label": "Table", "bbox": [10, 210, 320, 380], "text": html},
            {"label": "Page-Footer", "bbox": [10, 390, 300, 400], "text": "第 1 页"},
        ],
        width=400,
        height=420,
    )
    blocks = blocks_from_indra_page(page)

    assert [b.type for b in blocks] == ["title", "text", "table", "footer"]
    assert blocks[0].text == "年度报告"
    assert blocks[2].table is not None
    assert isinstance(blocks[2].table, Table)
    assert blocks[2].text.startswith("姓名\t年龄")
    assert blocks[1].bbox == [10.0, 50.0, 300.0, 200.0]


def test_empty_blocks_falls_back_to_lines():
    page = FakePage(
        blocks=[],
        lines=[
            {"box": [10, 10, 190, 30], "text": "第一行"},
            {"box": [10, 40, 180, 60], "text": "第二行"},
        ],
        width=200,
        height=100,
    )
    blocks = blocks_from_indra_page(page)
    assert len(blocks) == 1
    assert blocks[0].type == "text"
    assert blocks[0].text == "第一行\n第二行"
    assert blocks[0].bbox == [10.0, 10.0, 190.0, 60.0]


def test_table_structure_is_backfilled_from_markdown():
    """真实 Indra：Table 块只有逐行文本，HTML 表格在 page.markdown 里。"""
    html = "<table><tr><th>姓名</th><th>年龄</th></tr><tr><td>张三</td><td>18</td></tr></table>"
    page = FakePage(
        blocks=[{"label": "Table", "bbox": [0, 0, 200, 80], "text": "姓名\n年龄\n张三\n18"}],
        markdown="正文\n" + html + "\n结尾",
        width=200,
        height=100,
    )
    blocks = blocks_from_indra_page(page)
    assert blocks[0].type == "table"
    assert blocks[0].table is not None
    assert blocks[0].text.startswith("姓名\t年龄")


def test_furniture_blocks_can_be_filtered():
    page = FakePage(
        blocks=[
            {"label": "Page-Header", "bbox": [0, 0, 100, 10], "text": "页眉"},
            {"label": "Text", "bbox": [0, 20, 100, 50], "text": "正文"},
            {"label": "Page-Footer", "bbox": [0, 90, 100, 100], "text": "第 1 页"},
        ]
    )
    assert len(blocks_from_indra_page(page, keep_furniture=False)) == 1
    assert len(blocks_from_indra_page(page, keep_furniture=True)) == 3


def test_registry_includes_indra_when_enabled_and_installed(monkeypatch):
    monkeypatch.setattr(P, "indra_available", lambda: True)
    specs = P.build_registry(FakeSettings())
    ids = [s.id for s in specs]
    assert "INDRA" in ids
    spec = next(s for s in specs if s.id == "INDRA")
    assert spec.backend == "indra"
    assert spec.kind == "indra"
    assert spec.params == frozenset({"math", "keep_furniture"})


def test_registry_skips_indra_when_not_installed(monkeypatch):
    monkeypatch.setattr(P, "indra_available", lambda: False)
    specs = P.build_registry(FakeSettings())
    assert "INDRA" not in [s.id for s in specs]


def test_engine_predicts_with_fake_indra_module(tmp_path, monkeypatch):
    """验证 load/ocr 调用链与参数透传（用假模块，不需要真的 indra-ocr）。"""
    from backend.indra_engine import IndraEngine
    from backend.pipelines import PipelineSpec

    calls = {}
    page = FakePage(
        blocks=[{"label": "Text", "bbox": [0, 0, 100, 30], "text": "hello"}],
        width=100,
        height=30,
    )

    class FakeConv:
        def ocr(self, gray):
            calls["ocr_gray"] = gray
            return page

        def convert(self, path):
            return [page]

    fake = type(sys)("indra_ocr")

    def fake_load(**kwargs):
        calls["load"] = kwargs
        return FakeConv()

    fake.load = fake_load
    monkeypatch.setitem(sys.modules, "indra_ocr", fake)

    spec = PipelineSpec(
        id="INDRA", label="Indra OCR", arg="indra", kind="indra",
        params=frozenset({"math", "keep_furniture"}), backend="indra",
    )
    engine = IndraEngine(FakeSettings(), spec)
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")

    result = engine.predict_page(image, {"math": True, "keep_furniture": False})
    assert calls["load"]["math"] is True
    assert calls["load"]["furniture"] is False
    assert len(result.blocks) == 1
    assert result.blocks[0].text == "hello"
    assert result.width == 100 and result.height == 30
    assert engine.status()["state"] == "ready"
    assert engine.status()["predict_count"] == 1

