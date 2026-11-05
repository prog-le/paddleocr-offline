"""智能产线选择器。

用户上传文件时若未指定产线（``pipeline`` 参数为空 / ``"auto"``），
本模块会先探测文件类型，再（对 PDF）调用 pdf-inspector 的轻量
``classify_pdf`` 判断是原生文本还是扫描件，然后按策略选一条最合适的产线。

设计原则：

1. **尊重用户手动选择**：只要 ``pipeline`` 不是空/auto，直接返回该产线，
   不覆盖——用户明确选了就信任。
2. **先探测再路由**：不按扩展名瞎猜，pdfio.probe 先拿到真实 kind，
   PDF 再用 pdf-inspector.classify_pdf() 拿到分类。
3. **零模型依赖**：pdf-inspector 的 classify_pdf 比完整 process_pdf 还轻，
   不加载任何 OCR runtime / 模型文件。
4. **结果可解释**：返回 (spec, reason)，reason 直接写到任务日志里给用户看。
5. **可被禁用**：settings 里 ``ocr.auto_pipeline: false`` 关闭智能选择，
   退化成用 ``ocr.pipeline`` 默认产线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ocr.smart_router")

AUTO_TOKENS = {"", "auto", "AUTO", "recommend", "recommended", "智能", "智能选择", "auto_pipeline"}


@dataclass
class PipelineRecommendation:
    spec_id: str
    reason: str
    rationale: str  # 更详细的依据，用于任务日志
    pipeline_label: str = ""


def _try_classify_pdf(path: Path) -> Optional[Dict[str, Any]]:
    """轻量 PDF 分类，不加载 OCR runtime。失败返回 None（让调用方兜底）。"""
    try:
        import pdf_inspector  # noqa: WPS433
    except Exception:  # noqa: BLE001 - 没装 wheel，跳过
        return None
    try:
        c = pdf_inspector.classify_pdf(str(path))
        return {
            "pdf_type": getattr(c, "pdf_type", "unknown"),
            "confidence": float(getattr(c, "confidence", 0.0) or 0.0),
            "page_count": int(getattr(c, "page_count", 0) or 0),
            "pages_needing_ocr": list(getattr(c, "pages_needing_ocr", []) or []),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("smart_router: classify_pdf 失败 %s：%s", path, exc)
        return None


def _spec_by_id(specs: List[Any], spec_id: str) -> Optional[Any]:
    for s in specs:
        if getattr(s, "id", "") == spec_id:
            return s
    return None


def select_pipeline(
    requested_pipeline: Optional[str],
    kind: str,
    src_path: Path,
    specs: List[Any],
    settings: Any,
) -> PipelineRecommendation:
    """选一条产线。

    :param requested_pipeline: 前端传的产线 id；为空或在 AUTO_TOKENS 里表示
        需要智能选择。
    :param kind: pdfio.probe 返回的 ``pdf`` / ``image``。
    :param src_path: 上传文件路径（仅对 PDF 做轻量分类需要）。
    :param specs: build_registry(settings) 产出的产线注册表。
    :param settings: Settings()，用于读取 ``ocr.auto_pipeline`` /
        ``ocr.pipeline`` / ``pdf_inspector.enabled`` / ``indra.enabled``。
    """
    # ---- 1) 用户显式选了产线就尊重 ----
    if requested_pipeline and requested_pipeline not in AUTO_TOKENS:
        spec = _spec_by_id(specs, requested_pipeline)
        if spec is not None:
            return PipelineRecommendation(
                spec_id=spec.id,
                reason="用户手动选择",
                rationale=f"前端指定 pipeline={requested_pipeline}",
                pipeline_label=getattr(spec, "label", spec.id),
            )
        # 没找到这条产线（可能被 available_pipelines 限掉 / 没装 wheel）
        # 不立刻失败，走智能路由找最接近的，原因里说明用户选的那条不可用

    # ---- 2) auto_pipeline 开关允许？否则直接用默认产线 ----
    auto_enabled = bool(settings.get("ocr.auto_pipeline", True) if settings else True)
    default_id = str(settings.get("ocr.pipeline", "PP-StructureV3") or "PP-StructureV3") if settings else "PP-StructureV3"
    default_spec = _spec_by_id(specs, default_id) or specs[0] if specs else None
    if default_spec is None:
        return PipelineRecommendation(
            spec_id="", reason="无可选产线", rationale="specs 为空",
        )

    if not auto_enabled:
        return PipelineRecommendation(
            spec_id=default_spec.id,
            reason="智能产线已关闭",
            rationale=f"ocr.auto_pipeline=false，按默认产线 {default_spec.id}",
            pipeline_label=getattr(default_spec, "label", default_spec.id),
        )

    # ---- 3) 非 PDF 文件直接走默认 ----
    if kind != "pdf":
        return PipelineRecommendation(
            spec_id=default_spec.id,
            reason=f"非 PDF 文件（kind={kind}）",
            rationale=f"pdfio.probe 返回 kind={kind}，不调用 pdf-inspector 分类，走默认产线 {default_spec.id}",
            pipeline_label=getattr(default_spec, "label", default_spec.id),
        )

    # ---- 4) PDF：用 pdf-inspector 轻量分类 ----
    cls = _try_classify_pdf(src_path)
    if cls is None:
        return PipelineRecommendation(
            spec_id=default_spec.id,
            reason="pdf-inspector 未安装或分类失败",
            rationale=f"classify_pdf 不可用，走默认产线 {default_spec.id}；可 pip install pdf-inspector 获得智能选择",
            pipeline_label=getattr(default_spec, "label", default_spec.id),
        )

    pdf_type = cls["pdf_type"]
    confidence = cls["confidence"]
    pages_ocr = cls["pages_needing_ocr"]
    ocr_pages_n = len(pages_ocr)
    page_count = cls["page_count"] or 0
    ocr_ratio = (ocr_pages_n / page_count) if page_count > 0 else 0.0

    # ---- 5) 规则 ----
    pdfinspector_spec = _spec_by_id(specs, "PDFINSPECTOR")
    indra_spec = _spec_by_id(specs, "INDRA")

    # 5a) text_based 且 PDFINSPECTOR 可用 → 快抽
    # 注意：pdf-inspector 对原生文本 PDF 经常只给 0.5 左右的 confidence，
    # 把阈值放低到 0.4 才能稳定命中；若后面扩展 confidence 阈值更严格，
    # 可以再调回去。
    if pdf_type == "text_based" and pdfinspector_spec is not None and confidence >= 0.4:
        return PipelineRecommendation(
            spec_id=pdfinspector_spec.id,
            reason="原生文本 PDF，Rust 快抽秒级处理",
            rationale=(
                f"classify_pdf → text_based（置信度 {confidence:.0%}），"
                f"页面 {page_count} 全部有原生文本，"
                f"PDFINSPECTOR ~150 ms/页，比 OCR 快一个数量级。"
                f"若其中混有扫描页（{ocr_pages_n} 页需 OCR），"
                f"换 PaddleX 产线可自动处理。"
            ),
            pipeline_label=getattr(pdfinspector_spec, "label", pdfinspector_spec.id),
        )

    # 5b) 全/多扫描页 + Indra 可用 → 轻量 Indra
    if pdf_type in ("scanned", "image_based") and indra_spec is not None:
        return PipelineRecommendation(
            spec_id=indra_spec.id,
            reason="扫描件，Indra OCR 轻量 CPU 模型",
            rationale=(
                f"classify_pdf → {pdf_type}（置信度 {confidence:.0%}），"
                f"全 {page_count} 页需 OCR；"
                f"INDRA 模型仅 ~76 MB，CPU 约 0.2~1 s/页，"
                f"比 PaddleX 产线启动更快、内存更低。"
            ),
            pipeline_label=getattr(indra_spec, "label", indra_spec.id),
        )

    # 5c) mixed（部分页需要 OCR）→ PDFINSPECTOR 快抽纯文本页 + 日志提示
    if pdf_type == "mixed" and pdfinspector_spec is not None and ocr_ratio < 0.3:
        return PipelineRecommendation(
            spec_id=pdfinspector_spec.id,
            reason="混合页（大部分原生文本），快抽优先",
            rationale=(
                f"classify_pdf → mixed（置信度 {confidence:.0%}），"
                f"{ocr_pages_n}/{page_count} 页需 OCR（占比 {ocr_ratio:.0%}）；"
                f"PDFINSPECTOR 快抽原生文本页，"
                f"扫描页会保留原样供后续 OCR——若要全量处理扫描页请手动选 PaddleX 产线。"
            ),
            pipeline_label=getattr(pdfinspector_spec, "label", pdfinspector_spec.id),
        )

    # 5d) 兜底：PaddleX 默认产线
    return PipelineRecommendation(
        spec_id=default_spec.id,
        reason="扫描件 / 图片 / 混合页（需 OCR）",
        rationale=(
            f"classify_pdf → {pdf_type}（置信度 {confidence:.0%}），"
            f"{ocr_pages_n}/{page_count} 页需 OCR；"
            f"走 {default_spec.id} 自动 OCR + 版面分析。"
        ),
        pipeline_label=getattr(default_spec, "label", default_spec.id),
    )
