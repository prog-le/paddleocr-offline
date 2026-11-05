"""Indra OCR 产线适配层（可选）。

Indra OCR 是 Datalab 的 1M 参数轻量 OCR 模型（ONNX Runtime，纯 CPU），
输出按阅读顺序排列的版面块（标题/正文/表格/图注/页眉页脚）与文本行框。

本模块把它的结果归一化为项目统一的 ``Block`` 模型，使其能复用现有的
任务队列、进度回报、网页预览与四种格式导出，而不改动上层流程。

设计要点：
  * 依赖 ``indra-ocr`` 为**可选依赖**：未安装时产线不会注册，PaddleX 产线不受影响；
  * 模型目录可用 ``indra.model_dir`` / ``INDRA_OCR_MODEL_DIR`` 指向离线目录，
    为空时交给 indra-ocr 自己的缓存逻辑（首次运行会从 Hugging Face 下载约 76 MB）；
  * 每页推理使用进程内单实例 + 互斥锁（模型很小，但 ``conv`` 不保证可重入）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .docmodel import Block
from .engine import EngineError, PageResult, to_python
from .logging_setup import get_logger
from .settings import PROJECT_ROOT, Settings
from .tableparse import Table, looks_like_html_table, parse_markdown_table, parse_table_html

# 允许前端透传的 Indra 参数（其余 PaddleX 参数对它无意义）
INDRA_PARAMS = {"math", "keep_furniture"}

# Indra 版面块标签 -> 项目归一化类型
LABEL_TYPE_MAP = {
    "text": "text",
    "section-header": "title",
    "page-header": "header",
    "page-footer": "footer",
    "list-group": "text",
    "table": "table",
    "caption": "caption",
    "footnote": "noise",
    "table-of-contents": "text",
    "form": "text",
}

_HTML_TABLE_RE = re.compile(r"<table[^>]*>[\s\S]*?</table>", re.IGNORECASE)


def indra_available() -> bool:
    """是否安装了 indra-ocr（只查模块，不真正导入，避免拉起 onnxruntime）。"""
    try:
        return importlib.util.find_spec("indra_ocr") is not None
    except Exception:  # noqa: BLE001
        return False


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _bbox(value: Any) -> List[float]:
    """[x0, y0, x1, y1] / 多边形 -> [x0, y0, x1, y1]。"""
    pts = to_python(value)
    if not isinstance(pts, list) or not pts:
        return [0.0, 0.0, 0.0, 0.0]
    if len(pts) == 4 and all(isinstance(v, (int, float)) for v in pts):
        return [float(v) for v in pts]
    xs: List[float] = []
    ys: List[float] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(xs), min(ys), max(xs), max(ys)]


def _table_from_text(text: str) -> Optional[Table]:
    """从块文本提取表格结构（兼容个别版本把 HTML table 直接放进 block.text）。"""
    match = _HTML_TABLE_RE.search(text or "")
    candidates = [match.group(0)] if match else []
    if text and text.strip().startswith("|"):
        candidates.append(text)
    for candidate in candidates:
        if looks_like_html_table(candidate):
            table = parse_table_html(candidate)
        else:
            table = parse_markdown_table(candidate)
        if not table.is_empty:
            return table
    return None


def _tables_from_markdown(markdown: str) -> List[Table]:
    """Indra 的表格结构只出现在 markdown 里（HTML table），按出现顺序提取。"""
    tables: List[Table] = []
    for match in _HTML_TABLE_RE.finditer(markdown or ""):
        try:
            table = parse_table_html(match.group(0))
        except Exception:  # noqa: BLE001 - 坏表格跳过，不影响其它块
            continue
        if not table.is_empty:
            tables.append(table)
    return tables


def blocks_from_indra_page(page: Any, keep_furniture: bool = True) -> List[Block]:
    """Indra ``Page`` -> 项目 ``Block`` 列表（保持其阅读顺序）。

    优先使用 ``page.blocks``（带版面标签与坐标）；表格块自身只有逐行文本，
    真正的 HTML 表格在 ``page.markdown`` 里，因此按出现顺序回填到 Table 块。
    极少数没有 blocks 的模型/页面，退回文本行拼成的单块，保证不会"识别成功但结果为空"。
    """
    markdown_tables = _tables_from_markdown(str(_get(page, "markdown", "") or ""))
    table_index = 0
    blocks: List[Block] = []
    raw_blocks = _get(page, "blocks") or []
    for index, item in enumerate(raw_blocks):
        label = str(_get(item, "label", "Text") or "Text")
        lowered = label.strip().lower()
        if not keep_furniture and lowered in ("page-header", "page-footer"):
            continue
        btype = LABEL_TYPE_MAP.get(lowered, "text")
        text = str(_get(item, "text", "") or "")
        block = Block(
            type=btype,
            label=label,
            bbox=_bbox(_get(item, "bbox")),
            text=text,
            order=index,
        )
        if btype == "table":
            table = _table_from_text(text)
            if table is None and table_index < len(markdown_tables):
                table = markdown_tables[table_index]
            table_index += 1
            if table is not None:
                block.table = table
                block.text = table.to_tsv()
            elif not text.strip():
                continue  # 空表格块不保留，避免导出空表
        if not block.text.strip() and block.table is None:
            continue
        blocks.append(block)

    if blocks:
        return blocks

    lines = _get(page, "lines") or []
    text = "\n".join(str(_get(line, "text", "") or "") for line in lines).strip()
    if not text:
        text = str(_get(page, "markdown", "") or "").strip()
    if text:
        bbox = [0, 0, 0, 0]
        if lines:
            boxes = [_bbox(_get(line, "box")) for line in lines]
            bbox = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
        blocks.append(Block(type="text", label="text", bbox=bbox, text=text, order=0))
    return blocks


class IndraEngine:
    """Indra OCR 单实例引擎（CPU / ONNX Runtime）。"""

    is_mock = False

    def __init__(self, settings: Settings, spec: Any = None):
        self.settings = settings
        self.spec = spec
        self.pipeline_id = getattr(spec, "id", "INDRA")
        self.pipeline_name = self.pipeline_id
        self.label = getattr(spec, "label", "Indra OCR")
        self.kind = getattr(spec, "kind", "indra")
        self.log = get_logger("ocr.indra")
        self.concurrency = max(int(settings.get("ocr.concurrency", 1) or 1), 1)
        self.device = "cpu"
        self.device_info = {
            "requested": "cpu",
            "device": "cpu",
            "kind": "cpu",
            "name": "CPU（ONNX Runtime / Indra OCR）",
            "fallback": False,
            "warnings": [],
        }
        self.model_dir = self._resolve_model_dir()
        self._lock = threading.RLock()
        self._conv: Any = None
        self._conv_key: Optional[tuple] = None
        self._status: Dict[str, Any] = {
            "state": "idle",  # idle | loading | ready | error
            "pipeline": self.pipeline_id,
            "label": self.label,
            "device": self.device,
            "device_requested": "cpu",
            "device_info": self.device_info,
            "loaded": False,
            "instances": 0,
            "error": None,
            "loaded_at": None,
            "load_seconds": None,
            "predict_count": 0,
            "last_predict_ms": None,
        }

    # ------------------------------------------------------------------ 配置
    def _resolve_model_dir(self) -> Optional[Path]:
        raw = os.environ.get("INDRA_OCR_MODEL_DIR") or self.settings.get("indra.model_dir", "")
        if not raw:
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return path

    def _default_key(self) -> tuple:
        return self._params_key({})

    def _params_key(self, params: Dict[str, Any]) -> tuple:
        defaults = self.settings.get("indra", {}) or {}
        math = params.get("math", defaults.get("math", False))
        furniture = params.get("keep_furniture", defaults.get("keep_furniture", False))
        return (bool(math), bool(furniture))

    @property
    def params_allowed(self) -> set:
        if self.spec is not None:
            return set(getattr(self.spec, "params", INDRA_PARAMS))
        return set(INDRA_PARAMS)

    def _load_converter(self, key: tuple) -> Any:
        try:
            import indra_ocr
        except Exception as exc:  # noqa: BLE001
            raise EngineError(
                "未安装 indra-ocr，无法使用 Indra OCR 产线。请在有外网的机器执行 "
                "`pip install -r requirements-indra.txt`（或 `pip install indra-ocr`）后重启服务；"
                f"原始错误：{exc}"
            ) from exc
        math, furniture = key
        kwargs: Dict[str, Any] = {"math": math, "furniture": furniture}
        if self.model_dir is not None:
            kwargs["path"] = str(self.model_dir)
        try:
            threads = int(self.settings.get("indra.threads", 0) or 0)
        except (TypeError, ValueError):
            threads = 0
        if threads > 0:
            kwargs["threads"] = threads
        self.log.info(
            "加载 Indra OCR（模型目录 %s，threads=%s，math=%s，furniture=%s）",
            self.model_dir or "包内默认缓存",
            threads or "默认(4)",
            math,
            furniture,
        )
        try:
            return indra_ocr.load(**kwargs)
        except TypeError:
            # 兼容旧版本 load() 不认识的开关：退回最核心参数
            slim = {k: v for k, v in kwargs.items() if k in ("path", "threads")}
            return indra_ocr.load(**slim)

    def _get_converter(self, params: Dict[str, Any]) -> Any:
        key = self._params_key(params)
        if self._conv is not None and self._conv_key == key:
            return self._conv
        self._conv = self._load_converter(key)
        self._conv_key = key
        return self._conv

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set_status(self, **kwargs: Any) -> None:
        with self._lock:
            self._status.update(kwargs)

    def ensure_loaded(self) -> None:
        """按默认配置预加载模型（预热）。失败抛 EngineError。"""
        if self.status()["state"] == "ready":
            return
        started = time.time()
        self._set_status(state="loading", error=None)
        try:
            with self._lock:
                self._get_converter({})
        except EngineError as exc:
            self._set_status(state="error", error=str(exc), loaded=False)
            self.log.error("Indra OCR 加载失败：%s", exc)
            raise
        self._set_status(
            state="ready",
            loaded=True,
            instances=1,
            loaded_at=time.time(),
            load_seconds=round(time.time() - started, 2),
        )
        self.log.info("Indra OCR 已加载，耗时 %.1fs", time.time() - started)

    def model_inventory(self) -> Dict[str, Any]:
        directory = self.model_dir
        if directory is None:
            return {
                "dir": "（包内默认缓存）",
                "exists": indra_available(),
                "models": [],
                "package": "indra-ocr",
                "note": "未配置 indra.model_dir；首次运行会从 Hugging Face 下载约 76 MB 模型",
            }
        models: List[Dict[str, Any]] = []
        if directory.exists():
            for child in sorted(directory.iterdir()):
                if child.is_dir():
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                elif child.is_file():
                    size = child.stat().st_size
                else:
                    continue
                models.append({"name": child.name, "size_mb": round(size / 1024 / 1024, 1)})
        return {
            "dir": str(directory),
            "exists": directory.exists(),
            "models": models,
            "package": "indra-ocr",
        }

    def close(self) -> None:
        with self._lock:
            self._conv = None
            self._conv_key = None
            self._status.update(state="idle", loaded=False, instances=0)

    # ------------------------------------------------------------------ 推理
    def _run(self, conv: Any, image_path: Path) -> Any:
        """优先用灰度图直接识别；失败时退回按文件转换。"""
        page = None
        try:
            import cv2

            gray = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if gray is not None and hasattr(conv, "ocr"):
                page = conv.ocr(gray)
        except Exception as exc:  # noqa: BLE001 - 退回文件路径模式
            self.log.debug("Indra 灰度读取失败，改用文件模式：%s", exc)
        if page is None:
            pages = list(conv.convert(str(image_path)))
            if not pages:
                raise EngineError("Indra OCR 未返回页面结果")
            page = pages[0]
        return page

    def predict_page(self, image_path: Path, params: Dict[str, Any]) -> PageResult:
        started = time.time()
        with self._lock:
            try:
                conv = self._get_converter(params)
            except EngineError:
                self._set_status(state="error", error="Indra OCR 模型加载失败", loaded=False)
                raise
            try:
                page = self._run(conv, image_path)
            except EngineError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise EngineError(f"Indra OCR 推理失败：{exc}") from exc

            blocks = blocks_from_indra_page(page, keep_furniture=bool(self._params_key(params)[1]))
            elapsed_ms = int((time.time() - started) * 1000)
            width = int(_get(page, "width", 0) or 0)
            height = int(_get(page, "height", 0) or 0)
            raw = to_python(page)
            if not isinstance(raw, dict):
                raw = {"raw": raw}
            self._status.update(
                state="ready",
                loaded=True,
                instances=1,
                predict_count=int(self._status.get("predict_count") or 0) + 1,
                last_predict_ms=elapsed_ms,
            )
            return PageResult(
                blocks=blocks,
                width=width,
                height=height,
                raw=raw,
                elapsed_ms=elapsed_ms,
            )
