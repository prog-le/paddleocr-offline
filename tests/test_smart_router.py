"""backend/smart_router.py 单测。

不需要网络 / 不需要 PaddleX 运行时 / 不需要 OCR 模型。
依赖：pdf-inspector（pip install pdf-inspector）——没有时会跳过依赖它的 PDF 分类场景。
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.smart_router import AUTO_TOKENS, PipelineRecommendation, select_pipeline  # noqa: E402

try:
    import pdf_inspector  # noqa: WPS433

    PDFI_OK = True
except Exception:  # noqa: BLE001
    PDFI_OK = False


class _FakeSettings:
    """Settings 的 duck-type 替身：只实现 smart_router 需要的 .get()。"""

    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}

    def get(self, dotted: str, default=None):
        return self.overrides.get(dotted, default)


FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    if ok:
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def _mk_text_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 16); c.drawString(72, 720, "Quarterly Report")
    c.setFont("Helvetica", 10); c.drawString(72, 690, "This is a normal paragraph with some body text.")
    c.save()


def _mktmp_pdf() -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    p = Path(tmp.name)
    _mk_text_pdf(p)
    return p


def _fake_spec(id_: str, label: str = "") -> object:
    class S:
        def __init__(self, id_, label=""):
            self.id = id_
            self.label = label or id_

    return S(id_, label)


SPECS = [
    _fake_spec("OCR", "纯文字识别"),
    _fake_spec("PDFINSPECTOR", "PDF Inspector"),
    _fake_spec("PP-StructureV3", "通用版面解析"),
    _fake_spec("PP-StructureV3-full", "通用版面解析（完整）"),
    _fake_spec("table_recognition_v2", "通用表格识别"),
    _fake_spec("INDRA", "Indra OCR"),
]


def test_user_explicit_honored() -> None:
    print("\n用户显式选产线 → 尊重")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = _mktmp_pdf()
    try:
        rec = select_pipeline(
            requested_pipeline="OCR",
            kind="pdf",
            src_path=p,
            specs=SPECS,
            settings=s,
        )
        check("spec_id=OCR", rec.spec_id == "OCR", f"got {rec.spec_id}")
        check("reason=用户手动选择", rec.reason == "用户手动选择", f"got {rec.reason}")
    finally:
        try: p.unlink()
        except OSError: pass


def test_auto_token_equivalence() -> None:
    print("\n各种空/auto token 都触发智能路由")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = _mktmp_pdf()
    try:
        auto_id = None
        for tok in AUTO_TOKENS:
            rec = select_pipeline(
                requested_pipeline=tok,
                kind="pdf",
                src_path=p,
                specs=SPECS,
                settings=s,
            )
            if auto_id is None:
                auto_id = rec.spec_id
            else:
                check(f"token={tok} → 同一产线", rec.spec_id == auto_id, f"got {rec.spec_id} vs {auto_id}")
    finally:
        try: p.unlink()
        except OSError: pass


def test_image_kind_default() -> None:
    print("\nkind=image → 直接走默认产线")
    s = _FakeSettings({"ocr.auto_pipeline": True, "ocr.pipeline": "PP-StructureV3"})
    p = Path("/nonexistent.jpg")  # image 分支不 probe 文件
    rec = select_pipeline("", "image", p, SPECS, s)
    check("spec_id=PP-StructureV3", rec.spec_id == "PP-StructureV3", f"got {rec.spec_id}")
    check("reason=非 PDF 文件", rec.reason.startswith("非 PDF"))


def test_auto_pipeline_disabled() -> None:
    print("\nocr.auto_pipeline=false → 强制默认")
    s = _FakeSettings({"ocr.auto_pipeline": False, "ocr.pipeline": "OCR"})
    p = _mktmp_pdf()
    try:
        rec = select_pipeline(
            requested_pipeline="",
            kind="pdf",
            src_path=p,
            specs=SPECS,
            settings=s,
        )
        check("spec_id=OCR", rec.spec_id == "OCR", f"got {rec.spec_id}")
        check("reason=智能产线已关闭", rec.reason.startswith("智能产线已关闭"))
    finally:
        try: p.unlink()
        except OSError: pass


def test_text_based_pdf_picks_pdfinspector() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\ntext_based PDF → PDFINSPECTOR")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = _mktmp_pdf()
    try:
        rec = select_pipeline("", "pdf", p, SPECS, s)
        check("spec_id=PDFINSPECTOR", rec.spec_id == "PDFINSPECTOR", f"got {rec.spec_id}")
        check("reason 含 '原生文本'", "原生文本" in rec.reason, rec.reason)
        check("rationale 非空且可解释", len(rec.rationale) > 20)
    finally:
        try: p.unlink()
        except OSError: pass


def test_scanned_pdf_picks_indra() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\n模拟 scanned PDF（mock classify_pdf）→ INDRA")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = Path("/tmp/fake.pdf")

    class C:
        pdf_type = "scanned"
        confidence = 0.95
        page_count = 3
        pages_needing_ocr = [0, 1, 2]

    with patch("pdf_inspector.classify_pdf", return_value=C()):
        rec = select_pipeline("", "pdf", p, SPECS, s)
    check("spec_id=INDRA", rec.spec_id == "INDRA", f"got {rec.spec_id}")
    check("reason 含 '扫描件'", "扫描件" in rec.reason, rec.reason)


def test_mostly_text_pdf_mixed_picks_pdfinspector() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\nmixed PDF 但大部分原生文本 → PDFINSPECTOR")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = Path("/tmp/fake2.pdf")

    class C:
        pdf_type = "mixed"
        confidence = 0.85
        page_count = 10
        pages_needing_ocr = [7]  # 1/10 页

    with patch("pdf_inspector.classify_pdf", return_value=C()):
        rec = select_pipeline("", "pdf", p, SPECS, s)
    check("spec_id=PDFINSPECTOR（ocr_ratio=0.1 < 0.3）", rec.spec_id == "PDFINSPECTOR", f"got {rec.spec_id}")


def test_mostly_ocr_pdf_paddlefallback() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\nmixed PDF 但 60% 需 OCR → 兜底默认（PP-StructureV3）")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = Path("/tmp/fake3.pdf")

    class C:
        pdf_type = "mixed"
        confidence = 0.85
        page_count = 10
        pages_needing_ocr = [0, 1, 2, 3, 4, 5]  # 6/10 页

    with patch("pdf_inspector.classify_pdf", return_value=C()):
        rec = select_pipeline("", "pdf", p, SPECS, s)
    check("spec_id=PP-StructureV3（兜底）", rec.spec_id == "PP-StructureV3", f"got {rec.spec_id}")


def test_user_request_nonexistent_spec_falls_back() -> None:
    print("\n用户选了不可用产线 → 走智能路由")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = _mktmp_pdf()
    try:
        rec = select_pipeline("NOT_A_PIPELINE", "pdf", p, SPECS, s)
        # 不会报错，走智能路由兜底
        check("拿到有效 spec_id", bool(rec.spec_id) and rec.spec_id in {s.id for s in SPECS}, f"got {rec.spec_id}")
    finally:
        try: p.unlink()
        except OSError: pass


def test_no_pdfinspector_spec_skips_it() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\nPDFINSPECTOR 产线不在 specs 列表 → 不选它")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = _mktmp_pdf()
    small_specs = [s for s in SPECS if s.id != "PDFINSPECTOR"]
    try:
        rec = select_pipeline("", "pdf", p, small_specs, s)
        check("spec_id != PDFINSPECTOR", rec.spec_id != "PDFINSPECTOR", f"got {rec.spec_id}")
        # 应该落到 INDRA（scanned fallback）或 PaddleX 默认
        check("落到默认", rec.spec_id in {"INDRA", "PP-StructureV3", "OCR"}, f"got {rec.spec_id}")
    finally:
        try: p.unlink()
        except OSError: pass


def test_no_indra_spec_on_scanned() -> None:
    if not PDFI_OK:
        print("\n跳过（未安装 pdf-inspector）")
        return
    print("\nscanned PDF 但 INDRA 不可用 → 兜底默认产线")
    s = _FakeSettings({"ocr.auto_pipeline": True})
    p = Path("/tmp/fake4.pdf")

    class C:
        pdf_type = "scanned"
        confidence = 0.95
        page_count = 3
        pages_needing_ocr = [0, 1, 2]

    no_indra = [s for s in SPECS if s.id != "INDRA"]
    with patch("pdf_inspector.classify_pdf", return_value=C()):
        rec = select_pipeline("", "pdf", p, no_indra, s)
    check("spec_id=默认 PaddleX（PP-StructureV3）", rec.spec_id == "PP-StructureV3", f"got {rec.spec_id}")


def main() -> int:
    test_user_explicit_honored()
    test_auto_token_equivalence()
    test_image_kind_default()
    test_auto_pipeline_disabled()
    test_user_request_nonexistent_spec_falls_back()
    test_text_based_pdf_picks_pdfinspector()
    test_scanned_pdf_picks_indra()
    test_mostly_text_pdf_mixed_picks_pdfinspector()
    test_mostly_ocr_pdf_paddlefallback()
    test_no_pdfinspector_spec_skips_it()
    test_no_indra_spec_on_scanned()
    print()
    if FAIL:
        print(f"失败 {FAIL} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
