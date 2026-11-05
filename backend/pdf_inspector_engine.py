"""pdf-inspector 产线适配层（可选）。

pdf-inspector（Firecrawl 出品，Rust 实现）给本项目补齐「智能路由 + 快抽」能力：
  * 先 ~20ms 分类：TextBased / Scanned / ImageBased / Mixed（0 置信度 0.0~1.0）；
  * TextBased 页 ~150ms 抽原生文本 + 结构化 Markdown（标题 H1-H4 / 列表 / 代码块 /
    表格 / 加粗斜体 / 超链接 / 自动阅读顺序），绕开昂贵 OCR；
  * Scanned / ImageBased / Mixed 页保留给 PaddleX 产线；
  * 默认 ``process_pdf()`` 不碰任何 OCR runtime（纯 Rust）。

本模块把它的 Markdown 输出转成项目统一的 ``Block`` 模型。

设计要点：
  * 依赖 ``pdf-inspector`` 为**可选依赖**：未安装时产线不会注册，
    PaddleX 产线不受影响；
  * 推理是 Rust 函数，GIL 在每次 ``process_pdf()`` 调用里释放；
  * 不需要模型目录 / 随包 OCR runtime / GPU / 任何 ONNX 资产——纯 wheel 即可跑。
"""

from __future__ import annotations

import importlib.util
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .docmodel import Block
from .engine import EngineError, PageResult
from .logging_setup import get_logger
from .settings import Settings
from .tableparse import Table, parse_markdown_table

# 允许前端透传的 pdf-inspector 参数（其余 PaddleX 参数对它无意义）
PDF_INSPECTOR_PARAMS = {"include_headers", "include_footers"}

# pdf-inspector Markdown -> Block.type 映射
HEADING_LEVEL_RE = re.compile(r"^(#{1,4})\s+")


def pdf_inspector_available() -> bool:
    """是否安装了 pdf-inspector（只查模块，不真正导入，避免拉起 Rust wheel）。"""
    try:
        return importlib.util.find_spec("pdf_inspector") is not None
    except Exception:  # noqa: BLE001
        return False


def _mk_block(btype: str, text: str, order: int, label: str = "") -> Block:
    return Block(
        type=btype,
        label=label or btype,
        bbox=[0.0, 0.0, 0.0, 0.0],
        text=text.strip(),
        order=order,
    )


def _split_markdown_sections(markdown: str) -> List[str]:
    """把 pdf-inspector 生成的 Markdown 切成"块"。

    规则（按 pdf-inspector 1.25 的输出观察）：
      * 标题 ``# H1`` / ``## H2`` / ``### H3`` / ``#### H4`` → 独立块；
      * 表格 ``| col | col |`` + ``|---|---|`` → 合并为表格块；
      * ``- item`` / ``* item`` / ``1. item`` / ``a) item`` → 列表块；
      * 引用 ``> quote`` → 引用块；
      * 围栏代码 `````lang`` → 代码块；
      * 其余连续普通行 → 正文块（合并后作为一个 text block）；
      * 空行 / ``<!-- Page N -->`` 分页标记 → 忽略；
      * ``---`` 分隔线 → 忽略。
    """
    lines = (markdown or "").splitlines()
    # 预处理：合并表格（表头 + 分隔行 + 数据行）
    merged: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        # 跳过纯空行与分页注释
        if not stripped or stripped.startswith("<!-- Page") or stripped == "---":
            i += 1
            continue
        # 表格：首行以 "|" 开头且包含多个 "|"
        if stripped.startswith("|") and stripped.count("|") >= 2:
            tbl_lines = [stripped]
            j = i + 1
            while j < len(lines):
                nl = lines[j].strip()
                if nl.startswith("|") and nl.count("|") >= 2:
                    tbl_lines.append(nl)
                    j += 1
                else:
                    break
            # 跳过中间的分隔行 ---|---
            merged.append("\n".join(tbl_lines))
            i = j
            continue
        merged.append(line)
        i += 1

    sections: List[str] = []
    buf_lines: List[str] = []
    buf_kind: Optional[str] = None  # plain | bullet | numbered | letter | quote | code

    def flush() -> None:
        if not buf_lines:
            return
        content = "\n".join(buf_lines).strip()
        if content:
            sections.append(content)
        buf_lines.clear()

    for line in merged:
        stripped = line.strip()
        if buf_kind == "code":
            # 代码块内部：保留所有原文，直到碰到闭合 ```
            buf_lines.append(line)
            if stripped.startswith("```"):
                flush()
                buf_kind = None
            continue
        if not stripped or stripped.startswith("<!-- Page") or stripped == "---":
            continue
        if HEADING_LEVEL_RE.match(stripped):
            flush()
            sections.append(stripped)
            buf_kind = None
            continue
        if stripped.startswith("```"):
            flush()
            buf_kind = "code"
            buf_lines.append(line)
            continue
        # 表格（头部以 | 开头且含至少 2 个 |，整行不是在当前 buf_kind 里继续追加的）
        head = stripped.splitlines()[0]
        if head.startswith("|") and head.count("|") >= 2:
            # 切换 kind 或首次遇到表格块都 flush 旧内容
            if buf_kind and buf_kind != "table":
                flush()
            buf_lines.append(line)
            buf_kind = "table"
            continue
        if re.match(r"^(\s*[-*+]|\s*\d+[.)]|\s*[a-zA-Z][.)])\s", stripped):
            kind = "bullet" if re.match(r"^\s*[-*+]\s", stripped) else (
                "numbered" if re.match(r"^\s*\d+[.)]\s", stripped) else "letter"
            )
            if buf_kind and buf_kind != kind:
                flush()
            buf_kind = kind
            buf_lines.append(line)
            continue
        if stripped.startswith(">"):
            if buf_kind and buf_kind != "quote":
                flush()
            buf_kind = "quote"
            buf_lines.append(line)
            continue
        # 普通正文
        if buf_kind and buf_kind != "plain":
            flush()
        buf_kind = "plain"
        buf_lines.append(line)

    flush()
    return sections


def markdown_to_blocks(markdown: str) -> List[Block]:
    """pdf-inspector Markdown -> 项目 ``Block`` 列表。"""
    blocks: List[Block] = []
    for index, section in enumerate(_split_markdown_sections(markdown)):
        stripped = section.strip()
        if not stripped:
            continue
        m = HEADING_LEVEL_RE.match(stripped)
        if m:
            level = len(m.group(1))
            text = HEADING_LEVEL_RE.sub("", stripped).strip()
            if not text:
                continue
            blocks.append(_mk_block("title", text, index, label=f"h{level}"))
            continue
        # 表格（第一行以 | 开头且含表头分隔）
        head = stripped.splitlines()[0].strip()
        if head.startswith("|") and head.count("|") >= 2 and "\n" in stripped:
            table = parse_markdown_table(stripped)
            if not table.is_empty:
                block = _mk_block("table", table.to_tsv(), index, label="table")
                block.table = table
                blocks.append(block)
                continue
            # 解析失败就退化为普通文本块
        blocks.append(_mk_block("text", stripped, index))
    return blocks


def _heading_type(conf: Optional[float], heading_ratio: Optional[float]) -> str:
    return "text"


class PdfInspectorEngine:
    """pdf-inspector 单实例引擎（Rust wheel，纯 CPU）。"""

    is_mock = False

    def __init__(self, settings: Settings, spec: Any = None):
        self.settings = settings
        self.spec = spec
        self.pipeline_id = getattr(spec, "id", "PDFINSPECTOR")
        self.pipeline_name = self.pipeline_id
        self.label = getattr(spec, "label", "PDF Inspector")
        self.kind = getattr(spec, "kind", "pdf_inspector")
        self.backend = getattr(spec, "backend", "pdf_inspector")
        self.log = get_logger("ocr.pdf_inspector")
        self._lock = threading.RLock()
        self._result_module: Any = None  # 延迟导入
        self._status: Dict[str, Any] = {
            "state": "idle",
            "pipeline": self.pipeline_id,
            "label": self.label,
            "device": "cpu",
            "device_requested": "cpu",
            "device_info": {
                "requested": "cpu",
                "device": "cpu",
                "kind": "cpu",
                "name": "CPU（pdf-inspector Rust wheel）",
                "fallback": False,
                "warnings": [],
            },
            "loaded": False,
            "instances": 1,  # Rust wheel 无需预加载模型
            "error": None,
            "loaded_at": None,
            "load_seconds": 0.0,
            "predict_count": 0,
            "last_predict_ms": None,
        }

    @property
    def params_allowed(self) -> set:
        if self.spec is not None:
            return set(getattr(self.spec, "params", PDF_INSPECTOR_PARAMS))
        return set(PDF_INSPECTOR_PARAMS)

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def ensure_loaded(self) -> None:
        """Rust wheel 无需预加载模型——这里只是导入并打印一行日志。"""
        if self.status()["state"] == "ready":
            return
        started = time.time()
        try:
            import pdf_inspector  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._status.update(state="error", loaded=False, error=str(exc))
            self.log.error("pdf-inspector 导入失败：%s", exc)
            raise EngineError(
                "未安装 pdf-inspector，无法使用 PDFINSPECTOR 产线。请在有外网机器执行 "
                "`pip install -r requirements-pdf-inspector.txt`（纯 Rust wheel，无额外依赖）后重启；"
                f"原始错误：{exc}"
            ) from exc
        with self._lock:
            self._status.update(
                state="ready",
                loaded=True,
                loaded_at=time.time(),
                load_seconds=round(time.time() - started, 3),
                error=None,
            )
        self.log.info("pdf-inspector 已可用，导入耗时 %.3fs", time.time() - started)

    def model_inventory(self) -> Dict[str, Any]:
        return {
            "dir": "（无，纯 Rust wheel，不依赖模型文件）",
            "exists": pdf_inspector_available(),
            "models": [],
            "package": "pdf-inspector",
            "note": "无需模型目录；纯原生抽取；若想接 OCR 运行时（选择性 OCR）需另行配置 PDFium + ONNX Runtime",
        }

    def close(self) -> None:
        with self._lock:
            self._status.update(state="idle", loaded=False)

    # ------------------------------------------------------------------ 推理
    def predict_pdf(self, pdf_path: Path, params: Dict[str, Any]) -> PageResult:
        """整 PDF 一次性解析（pdf-inspector 的优势：单 parse 多页面）。

        产线入口（routes/public.py）在遇到 PDF 时走 predict_pdf，
        图片则不会路由到这里（产线是 pdf-inspector 专属）。
        """
        self.ensure_loaded()
        import pdf_inspector  # noqa: WPS433

        started = time.time()
        try:
            result = pdf_inspector.process_pdf(str(pdf_path))
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"pdf-inspector 推理失败：{exc}") from exc

        blocks = markdown_to_blocks(result.markdown or "")
        elapsed_ms = int((time.time() - started) * 1000)

        self._status.update(
            state="ready",
            loaded=True,
            predict_count=int(self._status.get("predict_count") or 0) + 1,
            last_predict_ms=elapsed_ms,
        )
        raw = {
            "pdf_type": getattr(result, "pdf_type", ""),
            "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
            "page_count": int(getattr(result, "page_count", 0) or 0),
            "pages_needing_ocr": list(getattr(result, "pages_needing_ocr", []) or []),
            "pages_with_tables": list(getattr(result, "pages_with_tables", []) or []),
            "pages_with_columns": list(getattr(result, "pages_with_columns", []) or []),
            "processing_time_ms": int(getattr(result, "processing_time_ms", 0) or 0),
            "has_encoding_issues": bool(getattr(result, "has_encoding_issues", False)),
            "cmap_gaps": bool(getattr(result, "cmap_gaps", False)),
            "title": getattr(result, "title", "") or "",
            "author": getattr(result, "author", "") or "",
            "producer": getattr(result, "producer", "") or "",
            "ocr_reasons_by_page": [
                getattr(r, "reason", "") or str(r)
                for r in (getattr(result, "ocr_reasons_by_page", []) or [])
            ],
        }
        return PageResult(
            blocks=blocks,
            width=0,
            height=0,
            raw=raw,
            elapsed_ms=elapsed_ms,
        )
