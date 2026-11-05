"""pdf-inspector 产线适配层单测。

不需要网络、不需要 PaddleX 运行时、不需要 OCR 模型。
依赖：pip install -r requirements-pdf-inspector.txt（Rust wheel，~6.5 MB）。

覆盖：
  * pdf-inspector 可用 / 未安装两种路径
  * _split_markdown_sections 对真实 pdf-inspector Markdown 输出的切分
  * markdown_to_blocks 的标题 / 表格 / 列表 / 引用 / 普通正文
  * PdfInspectorEngine 的 status / model_inventory / predict_pdf
    （用 reportlab 生成临时 text-based PDF）
  * 分类结果透传到 PageResult.raw
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ----------------------------------------------------------------- 基础可用性
from backend.pdf_inspector_engine import (  # noqa: E402
    HEADING_LEVEL_RE,
    _split_markdown_sections,
    markdown_to_blocks,
    pdf_inspector_available,
)
from backend.settings import Settings  # noqa: E402

pdf_available = pdf_inspector_available()


def _mk_pdf(path: Path) -> None:
    """用 reportlab 造一份可控的 text-based PDF。"""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 20)
    c.drawString(72, 720, "Quarterly Report")
    c.setFont("Helvetica", 14)
    c.drawString(72, 690, "Q1 2025 Financial Summary")
    c.setFont("Helvetica", 10)
    c.drawString(72, 660, "This is a normal paragraph with some body text.")
    c.drawString(72, 644, "Another sentence follows on a new line.")
    c.drawString(72, 614, "- Item one")
    c.drawString(72, 598, "- Item two")
    c.drawString(72, 582, "* Alternative bullet")
    c.save()


def _mktmp_pdf() -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    p = Path(tmp.name)
    _mk_pdf(p)
    return p


FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    if ok:
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def test_markdown_split() -> None:
    print("\n_split_markdown_sections")

    md = textwrap.dedent(
        """\
        # H1 Title

        ## H2 subtitle

        | a | b |
        |---|---|
        | 1 | 2 |
        | 3 | 4 |

        Normal paragraph.
        Another line of same paragraph.

        - bullet A
        - bullet B

        > quoted line

        ```python
        def foo(): pass
        ```
        """
    )
    sections = _split_markdown_sections(md)
    print("  sections:", sections)
    check("切出 H1", sections[0].startswith("# H1 Title"))
    check("切出 H2", sections[1].startswith("## H2 subtitle"))
    # 表格是一个整体
    check("表格合并成一块", any("| a | b |" in s and "| 3 | 4 |" in s for s in sections))
    # 普通正文合并
    para = next(s for s in sections if s.startswith("Normal paragraph"))
    check("普通正文合并", "Another line" in para)
    # 列表保留多条
    bul = next(s for s in sections if s.startswith("- bullet A"))
    check("列表保留多条", "- bullet B" in bul)
    quo = next(s for s in sections if s.startswith("> quoted"))
    check("引用保留", "> quoted line" in quo)
    code = next(s for s in sections if s.startswith("```python"))
    check("代码块保留", "```python" in code and "def foo" in code)


def test_markdown_to_blocks() -> None:
    print("\nmarkdown_to_blocks（结构化）")
    md = textwrap.dedent(
        """\
        # Quarterly Report

        | category | budget | actual |
        |---|---|---|
        | staff | 100 | 90 |
        """
    )
    blocks = markdown_to_blocks(md)
    check("至少有 2 个 block", len(blocks) >= 2)
    titles = [b for b in blocks if b.type == "title"]
    check("有 title block", len(titles) == 1 and titles[0].text == "Quarterly Report")
    tables = [b for b in blocks if b.type == "table"]
    check("有 table block", len(tables) == 1 and getattr(tables[0], "table", None) is not None)


def test_engine_lifecycle() -> None:
    if not pdf_available:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\nPdfInspectorEngine 生命周期")
    from backend.pdf_inspector_engine import PdfInspectorEngine

    eng = PdfInspectorEngine(Settings())
    check("status initial 是 idle", eng.status()["state"] == "idle")
    check("model_inventory.exists 是 True", eng.model_inventory()["exists"] is True)
    eng.ensure_loaded()
    check("ensure_loaded 后 state=ready", eng.status()["state"] == "ready")
    check("instances 默认为 1", eng.status()["instances"] == 1)
    eng.close()
    check("close 后 state=idle", eng.status()["state"] == "idle")


def test_predict_text_based_pdf() -> None:
    if not pdf_available:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\npredict_pdf（text-based PDF）")
    from backend.pdf_inspector_engine import PdfInspectorEngine

    p = _mktmp_pdf()
    try:
        eng = PdfInspectorEngine(Settings())
        eng.ensure_loaded()
        res = eng.predict_pdf(p, {})
        check("blocks 非空", len(res.blocks) >= 2, f"got {len(res.blocks)}")
        check("raw.pdf_type=text_based", res.raw.get("pdf_type") == "text_based",
              f"pdf_type={res.raw.get('pdf_type')}")
        check("raw.confidence=1.0", abs(float(res.raw.get("confidence", 0)) - 1.0) < 0.01)
        check("pages_needing_ocr 为空", res.raw.get("pages_needing_ocr") == [])
        check("elapsed_ms > 0", res.elapsed_ms > 0)
        titles = [b for b in res.blocks if b.type == "title"]
        check("抓到标题 Quarterly Report", any("Quarterly Report" in b.text for b in titles))
    finally:
        try:
            p.unlink()
        except OSError:
            pass


def test_engine_bad_path() -> None:
    if not pdf_available:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\npredict_pdf（不存在的文件抛 EngineError）")
    from backend.engine import EngineError
    from backend.pdf_inspector_engine import PdfInspectorEngine

    eng = PdfInspectorEngine(Settings())
    eng.ensure_loaded()
    try:
        eng.predict_pdf(Path("/definitely/does/not/exist.pdf"), {})
        check("抛 EngineError", False, "没抛任何异常")
    except EngineError:
        check("抛 EngineError", True)
    except Exception as exc:  # noqa: BLE001
        check("抛 EngineError", False, f"抛的是 {type(exc).__name__}: {exc}")


def test_not_installed_grace() -> None:
    """_split_markdown_sections / markdown_to_blocks 是纯文本工具，
    不依赖 pdf-inspector wheel 本身——即使未装 wheel 也能用。"""
    print("\n未安装 pdf-inspector 时：纯文本工具可用")
    md = "# Title\n\nBody."
    blocks = markdown_to_blocks(md)
    check("至少有 2 个 block", len(blocks) >= 2)


def main() -> int:
    test_markdown_split()
    test_markdown_to_blocks()
    test_not_installed_grace()
    if pdf_available:
        test_engine_lifecycle()
        test_predict_text_based_pdf()
        test_engine_bad_path()
    else:
        print("\n[INFO] 未安装 pdf-inspector（tests/test_pdf_inspector_engine.py 后半部分未执行，可先 pip install -r requirements-pdf-inspector.txt 再跑）")
    print()
    if FAIL:
        print(f"失败 {FAIL} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
