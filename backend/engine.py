"""PaddleX 产线封装。

只调用 PaddleX 集成产线（不手写底层推理）：
  * PP-StructureV3 —— 版面检测 + OCR + 表格识别 + 公式识别，输出解析块顺序即阅读顺序
  * OCR            —— 纯文本检测识别（快速模式，无表格结构）

线程安全：
  PaddleX pipeline 实例不保证并发安全，这里用「实例池 + 信号量」实现并发：
  concurrency=1 时只有一个实例（CPU 推荐）；调大时会额外加载实例（内存占用线性增长）。

离线保证：
  模型从 models/paddlex_home/official_models 读取（由 settings.apply_paddlex_env 设置
  PADDLE_PDX_CACHE_HOME + 关闭模型源探测），运行期不会联网。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .docmodel import Block, normalize_label
from . import devices as device_mod
from .logging_setup import get_logger
from .settings import Settings
from .tableparse import Table, looks_like_html_table, parse_markdown_table, parse_table_html

# 允许透传给 pipeline.predict 的参数（PaddleX 版本间新增参数不会导致 TypeError）
STRUCTURE_PARAMS = {
    "use_doc_orientation_classify",
    "use_doc_unwarping",
    "use_textline_orientation",
    "use_general_ocr",
    "use_seal_recognition",
    "use_table_recognition",
    "use_formula_recognition",
    "use_chart_recognition",
    "format_block_content",
    "layout_threshold",
    "layout_nms",
    "layout_unclip_ratio",
    "layout_merge_bboxes_mode",
    "text_det_limit_side_len",
    "text_det_limit_type",
    "text_det_thresh",
    "text_det_box_thresh",
    "text_det_unclip_ratio",
    "text_rec_score_thresh",
    "use_wired_table_cells_trans_to_html",
    "use_wireless_table_cells_trans_to_html",
    "use_table_orientation_classify",
    "use_ocr_results_with_table_cells",
}
# 专用表格产线（table_recognition_v2）可调参数
TABLE_PARAMS = {
    "use_doc_orientation_classify",
    "use_doc_unwarping",
    "use_layout_detection",
    "text_det_limit_side_len",
    "text_det_limit_type",
    "text_det_thresh",
    "text_det_box_thresh",
    "text_det_unclip_ratio",
    "text_rec_score_thresh",
    "use_ocr_results_with_table_cells",
    "use_e2e_wired_table_rec_model",
    "use_e2e_wireless_table_rec_model",
    "use_table_orientation_classify",
    "use_wired_table_cells_trans_to_html",
    "use_wireless_table_cells_trans_to_html",
}
OCR_PARAMS = {
    "use_doc_orientation_classify",
    "use_doc_unwarping",
    "use_textline_orientation",
    "text_det_limit_side_len",
    "text_det_limit_type",
    "text_det_thresh",
    "text_det_box_thresh",
    "text_det_unclip_ratio",
    "text_rec_score_thresh",
}

PIPELINE_ALIASES = {
    "structure": "PP-StructureV3",
    "pp-structurev3": "PP-StructureV3",
    "pp_structurev3": "PP-StructureV3",
    "ocr": "OCR",
    "table": "table_recognition_v2",
    "table_recognition": "table_recognition_v2",
    "tablerecognitionv2": "table_recognition_v2",
}


class EngineError(RuntimeError):
    """产线加载或推理失败（消息直接展示给用户，需可读）。"""


@dataclass
class PageResult:
    blocks: List[Block] = field(default_factory=list)
    width: int = 0
    height: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0


def resolve_pipeline_name(name: str) -> str:
    key = (name or "").strip()
    return PIPELINE_ALIASES.get(key.lower(), key or "PP-StructureV3")


# ---------------------------------------------------------------------------
# 工具：numpy / 数组 / 各种对象 -> 纯 Python
# ---------------------------------------------------------------------------
def to_python(obj: Any, _depth: int = 0) -> Any:
    if _depth > 12:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_python(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_python(v, _depth + 1) for v in obj]
    for attr in ("tolist", "item"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return to_python(fn(), _depth + 1)
            except Exception:  # noqa: BLE001
                pass
    if hasattr(obj, "__dict__"):
        return to_python(vars(obj), _depth + 1)
    return str(obj)


def _bbox_of(points: Any) -> List[float]:
    """多边形/矩形 -> [x0,y0,x1,y1]。"""
    pts = to_python(points)
    if not isinstance(pts, list) or not pts:
        return [0, 0, 0, 0]
    if len(pts) == 4 and all(isinstance(v, (int, float)) for v in pts):
        return [float(v) for v in pts]
    xs: List[float] = []
    ys: List[float] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return [0, 0, 0, 0]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bbox_iou(a: List[float], b: List[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(ix1 - ix0, 0.0), max(iy1 - iy0, 0.0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
    area_b = max((bx1 - bx0) * (by1 - by0), 1e-6)
    return inter / (area_a + area_b - inter)


def _table_from_block(content: str, table_res_list: List[Dict[str, Any]]) -> Optional[Table]:
    """从块内容 / table_res_list 里取出表格结构。"""
    if looks_like_html_table(content):
        table = parse_table_html(content)
        if not table.is_empty:
            return table
    if content.strip().startswith("|"):
        table = parse_markdown_table(content)
        if not table.is_empty:
            return table
    # 按表格识别子结果兜底（format_block_content=False 时块内容只有纯文本）
    best: Optional[Table] = None
    for item in table_res_list:
        html = item.get("pred_html") or item.get("html") or ""
        if not html:
            continue
        table = parse_table_html(html)
        if not table.is_empty and (best is None or len(table.cells) > len(best.cells)):
            best = table
    return best


PAYLOAD_KEYS = ("rec_texts", "rec_text", "parsing_res_list", "dt_polys", "overall_ocr_res")
WRAPPER_KEYS = ("res", "result", "data", "output", "results")


def unwrap_result(data: Any) -> Dict[str, Any]:
    """兼容不同 PaddleX 版本的结果外层包装。

    实测 paddlex 3.7.x 的 ``res.json`` 形如 {"res": {...}}（OCR 产线与
    PP-StructureV3 都可能出现），而早期版本是扁平字典。这里统一剥掉包装层，
    否则取 rec_texts / parsing_res_list 会拿到 None，表现为"识别成功但结果为空"。
    """
    if not isinstance(data, dict):
        return {}
    if any(k in data for k in PAYLOAD_KEYS):
        return data
    for key in WRAPPER_KEYS:
        inner = data.get(key)
        if isinstance(inner, dict):
            if any(k in inner for k in PAYLOAD_KEYS):
                return inner
            return unwrap_result(inner)
    return data


def blocks_from_structure(data: Dict[str, Any]) -> List[Block]:
    """PP-StructureV3 结果 -> Block 列表（顺序即阅读顺序）。"""
    parsing = data.get("parsing_res_list") or []
    table_res = [t for t in (data.get("table_res_list") or []) if isinstance(t, dict)]
    blocks: List[Block] = []
    for idx, item in enumerate(parsing):
        if not isinstance(item, dict):
            continue
        label = str(item.get("block_label") or "text")
        btype = normalize_label(label)
        content = item.get("block_content")
        if content is None:
            content = item.get("block_text") or ""
        text = str(content if isinstance(content, str) else to_python(content))
        bbox = _bbox_of(item.get("block_bbox"))
        order = item.get("block_order")
        block = Block(
            type=btype,
            label=label,
            bbox=bbox,
            text=text,
            order=int(order) if isinstance(order, (int, float)) else None,
        )
        if btype == "table":
            table = _table_from_block(text, table_res)
            if table is not None:
                block.table = table
                if not text or looks_like_html_table(text) or text.strip().startswith("|"):
                    block.text = table.to_tsv()
            elif text.strip():
                block.type = "text"  # 没有结构就当普通文本，避免导出空表
        if block.type == "figure":
            block.text = text or "[图片]"
        if not block.text.strip() and block.table is None:
            continue
        blocks.append(block)
    return blocks


def blocks_from_table(data: Dict[str, Any]) -> List[Block]:
    """table_recognition_v2 结果 -> 表格 Block 列表。

    该产线只输出 ``table_res_list``（每张表一个 `pred_html` + `cell_box_list`），
    没有 ``parsing_res_list``；正文段落不解析。若整页没检测到表格（例如纯文字稿），
    退回 OCR 文本块，避免用户看到"识别成功但内容为空"。
    """
    table_res = [t for t in (data.get("table_res_list") or []) if isinstance(t, dict)]
    layout = data.get("layout_det_res") if isinstance(data.get("layout_det_res"), dict) else {}
    layout_boxes = [
        b
        for b in (layout.get("boxes") or [])
        if isinstance(b, dict) and str(b.get("label") or "").lower() in ("table", "表格")
    ]

    blocks: List[Block] = []
    for idx, item in enumerate(table_res):
        html = item.get("pred_html") or item.get("html") or ""
        if not html:
            continue
        table = parse_table_html(html)
        if table.is_empty:
            continue
        # 表格位置：优先用单元格框并集（cell_box_list 可能是多边形或矩形），
        # 其次用版面检测里第 idx 个 table 框
        cell_boxes = [
            _bbox_of(b)
            for b in (to_python(item.get("cell_box_list")) or [])
            if isinstance(b, (list, tuple)) and b
        ]
        cell_boxes = [b for b in cell_boxes if b[2] > b[0] and b[3] > b[1]]
        if cell_boxes:
            bbox = [
                min(b[0] for b in cell_boxes),
                min(b[1] for b in cell_boxes),
                max(b[2] for b in cell_boxes),
                max(b[3] for b in cell_boxes),
            ]
        elif idx < len(layout_boxes):
            bbox = _bbox_of(layout_boxes[idx].get("coordinate"))
        else:
            bbox = [0, 0, 0, 0]

        blocks.append(
            Block(
                type="table",
                label="table",
                bbox=bbox,
                text=table.to_tsv(),
                order=idx,
                table=table,
            )
        )

    if blocks:
        return blocks

    # 没识别到表格：退回整页 OCR 文本（该产线内部本就有 OCR 子产线）
    fallback = blocks_from_ocr(data)
    if fallback:
        return fallback
    return []


def blocks_from_ocr(data: Dict[str, Any]) -> List[Block]:
    """OCR 产线结果 -> 按行聚类成段落的 Block 列表。"""
    rec = data.get("overall_ocr_res") if isinstance(data.get("overall_ocr_res"), dict) else data
    texts = rec.get("rec_texts") or rec.get("rec_text") or []
    scores = rec.get("rec_scores") or rec.get("rec_score") or []
    polys = rec.get("rec_polys") or rec.get("dt_polys") or []
    lines: List[Tuple[str, float, List[float]]] = []
    for i, text in enumerate(texts):
        text = str(text or "").strip()
        if not text:
            continue
        score = float(scores[i]) if i < len(scores) else 0.0
        bbox = _bbox_of(polys[i]) if i < len(polys) else [0, 0, 0, 0]
        lines.append((text, score, bbox))
    if not lines:
        return []
    lines.sort(key=lambda x: (round(x[2][1] / 10.0), x[2][0]))

    heights = [max(b[3] - b[1], 1.0) for _, _, b in lines]
    median_h = sorted(heights)[len(heights) // 2]

    blocks: List[Block] = []
    cur_texts: List[str] = []
    cur_boxes: List[List[float]] = []
    cur_scores: List[float] = []

    def flush() -> None:
        if not cur_texts:
            return
        x0 = min(b[0] for b in cur_boxes)
        y0 = min(b[1] for b in cur_boxes)
        x1 = max(b[2] for b in cur_boxes)
        y1 = max(b[3] for b in cur_boxes)
        blocks.append(
            Block(
                type="text",
                label="text",
                bbox=[x0, y0, x1, y1],
                text="\n".join(cur_texts),
                score=sum(cur_scores) / len(cur_scores),
            )
        )
        cur_texts.clear()
        cur_boxes.clear()
        cur_scores.clear()

    prev_box: Optional[List[float]] = None
    for text, score, box in lines:
        if prev_box is not None:
            v_gap = box[1] - prev_box[3]
            overlap = min(box[2], prev_box[2]) - max(box[0], prev_box[0])
            min_w = max(min(box[2] - box[0], prev_box[2] - prev_box[0]), 1.0)
            same_para = v_gap < median_h * 0.9 and overlap / min_w > 0.25
            if not same_para:
                flush()
        cur_texts.append(text)
        cur_boxes.append(box)
        cur_scores.append(score)
        prev_box = box
    flush()
    return blocks


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class PaddleXEngine:
    """PaddleX 产线实例池。"""

    is_mock = False

    def __init__(self, settings: Settings, spec: Any = None):
        self.settings = settings
        # spec 来自 backend/pipelines.py 的注册表；为空时按旧配置兜底（单产线模式）
        self.spec = spec
        if spec is not None:
            self.pipeline_id = spec.id
            self.pipeline_name = spec.id
            self.label = getattr(spec, "label", spec.id)
            self.pipeline_arg = spec.arg
            self.kind = spec.kind
            self.backend = getattr(spec, "backend", "paddlex")
        else:
            self.pipeline_id = resolve_pipeline_name(settings.get("ocr.pipeline", "PP-StructureV3"))
            self.pipeline_name = self.pipeline_id
            self.label = self.pipeline_id
            self.pipeline_arg = self._resolve_pipeline_arg(settings, self.pipeline_name)
            self.kind = "ocr" if self.pipeline_name.upper() == "OCR" else "structure"
            self.backend = "paddlex"
        self.concurrency = max(int(settings.get("ocr.concurrency", 1) or 1), 1)
        self.log = get_logger("ocr.engine")

        # 设备解析：auto 优先加速卡；Apple Silicon 强制 CPU（Paddle 无 Metal 后端）。
        # 配置了不可用的加速卡时**不让服务启动失败**（否则后台都无法打开去改配置），
        # 而是记录 device_error，在真正加载产线时抛出可读错误。
        self.device_spec = str(settings.get("ocr.device", "auto") or "auto")
        self.device_error: str = ""
        try:
            info = device_mod.resolve(self.device_spec)
            self.device = info.device
            self.device_info = info.to_dict()
            for warning in info.warnings:
                self.log.warning("%s", warning)
            if info.fallback:
                self.log.warning("设备已降级：%s", info.reason)
        except device_mod.DeviceError as exc:
            self.device = "cpu"
            self.device_error = str(exc)
            self.device_info = {
                "requested": self.device_spec,
                "device": "cpu",
                "kind": "cpu",
                "name": "CPU",
                "fallback": True,
                "reason": str(exc),
                "warnings": [],
            }
            self.log.error("设备配置不可用：%s", exc)
        self._slots: "queue.Queue[Any]" = queue.Queue()
        for _ in range(self.concurrency):
            self._slots.put(None)
        self._create_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "state": "idle",  # idle | loading | ready | error
            "pipeline": self.pipeline_id,
            "label": self.label,
            "device": self.device,
            "device_requested": self.device_spec,
            "device_info": self.device_info,
            "loaded": False,
            "instances": 0,
            "error": None,
            "loaded_at": None,
            "load_seconds": None,
            "predict_count": 0,
            "last_predict_ms": None,
        }

    def _ocr_pipeline_config(self) -> Optional[Dict[str, Any]]:
        """读项目内 OCR yaml，并按配置覆盖 PP-OCRv6 档位与批处理大小。

        为什么不用改文件：`create_pipeline(config=dict)` 支持直接传配置字典，
        这样同一份 yaml 可以服务 tiny/small/medium 三档，也让 batch size 可运行时调整
        （改完需「重载产线」生效，模型会重新加载）。

        PP-OCRv6 的档位命名是 tiny/small/medium（没有 v5 的 server/mobile）。
        """
        path = Path(str(self.pipeline_arg))
        if not path.is_file():
            return None  # 内置产线名（官方默认已是 PP-OCRv6_medium），无需覆盖
        try:
            import yaml

            from .textio import read_text_file

            text = read_text_file(path, what="OCR 产线配置")
            if text is None:
                self.log.warning("OCR 产线配置不是可读文本，回退到官方默认档位：%s", path)
                return None
            cfg = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("读取 OCR 产线配置失败，回退到官方默认：%s", exc)
            return None
        if not isinstance(cfg, dict) or str(cfg.get("pipeline_name", "")).upper() != "OCR":
            return None

        tier = str(self.settings.get("ocr.ocr_tier", "medium") or "medium").strip().lower()
        if tier not in ("tiny", "small", "medium"):
            self.log.warning("ocr.ocr_tier=%s 非法，回退 medium", tier)
            tier = "medium"

        sub = cfg.setdefault("SubModules", {})
        det = sub.setdefault("TextDetection", {})
        rec = sub.setdefault("TextRecognition", {})
        det["model_name"] = f"PP-OCRv6_{tier}_det"
        rec["model_name"] = f"PP-OCRv6_{tier}_rec"
        # model_dir 必须为空，否则会去固定目录找旧档位权重
        det["model_dir"] = None
        rec["model_dir"] = None

        try:
            rec_batch = int(self.settings.get("ocr.text_rec_batch_size", 6) or 6)
        except (TypeError, ValueError):
            rec_batch = 6
        rec["batch_size"] = max(1, rec_batch)

        line = sub.get("TextLineOrientation")
        if isinstance(line, dict):
            try:
                line_batch = int(self.settings.get("ocr.textline_batch_size", 6) or 6)
            except (TypeError, ValueError):
                line_batch = 6
            line["batch_size"] = max(1, line_batch)

        self.log.info(
            "OCR 产线配置：PP-OCRv6_%s（识别 batch=%s，文本行 batch=%s）",
            tier,
            rec.get("batch_size"),
            (line or {}).get("batch_size"),
        )
        return cfg

    @staticmethod
    def _resolve_pipeline_arg(settings: Settings, name: str) -> str:
        """返回传给 create_pipeline 的参数：项目内置 YAML 优先，其次产线名。

        项目内置 config/pipelines/<name>.yaml 与官方配置的唯一差异是关闭了
        PP-DocBlockLayout 区域检测（该模型官方源不可下载，且不影响表格识别）。
        """
        explicit = str(settings.get("ocr.pipeline_config", "") or "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if not path.is_absolute():
                path = (Path(settings.config_path).parent.parent / path).resolve()
            if path.exists():
                return str(path)
            get_logger("ocr.engine").warning("配置的产线文件不存在，回退到官方配置：%s", path)
            return name
        candidate = settings.config_path.parent / "pipelines" / f"{name}.yaml"
        if candidate.exists():
            return str(candidate)
        return name

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)

    @property
    def params_allowed(self) -> set:
        if self.spec is not None:
            return set(self.spec.params)
        if self.kind == "ocr":
            return OCR_PARAMS
        if self.kind == "table":
            return TABLE_PARAMS
        return STRUCTURE_PARAMS

    # ------------------------------------------------------------------ 加载
    def _new_pipeline(self) -> Any:
        try:
            from paddlex import create_pipeline
        except Exception as exc:  # noqa: BLE001
            raise EngineError(
                "无法导入 paddlex，请确认已执行离线依赖安装（scripts/install_offline.sh）且 "
                f"paddlepaddle 与本机架构匹配。原始错误：{exc}"
            ) from exc
        if self.device_error:
            raise EngineError(self.device_error)

        self._apply_gpu_memory_limit()
        try:
            if self.kind == "ocr":
                cfg = self._ocr_pipeline_config()
                if cfg is not None:
                    pipeline = create_pipeline(config=cfg, device=self.device)
                else:
                    pipeline = create_pipeline(pipeline=self.pipeline_arg, device=self.device)
            else:
                pipeline = create_pipeline(pipeline=self.pipeline_arg, device=self.device)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(self._explain_load_error(exc)) from exc
        return pipeline

    def _apply_gpu_memory_limit(self) -> None:
        """把显存池上限落到 Paddle（每次创建实例前调用，幂等）。"""
        if self.device_info.get("kind") != "gpu":
            return
        try:
            limit_mb = int(self.settings.get("ocr.gpu_mem_limit_mb", 0) or 0)
            fraction = float(self.settings.get("ocr.gpu_mem_fraction", 0) or 0)
        except (TypeError, ValueError):
            return
        frac = device_mod.apply_gpu_memory_limit(
            int(self.device_info.get("index") or 0), limit_mb=limit_mb, fraction=fraction
        )
        if frac:
            self.log.info("显存池比例已设为 %.2f（limit=%sMB）", frac, limit_mb or "auto")

    def _explain_load_error(self, exc: Exception) -> str:
        msg = str(exc)
        models_dir = self.settings.official_models_dir
        hint = (
            f"产线 {self.label}（{self.pipeline_id}）加载失败：{msg}\n"
            f"设备：{self.device}（配置 {self.device_spec}）  模型目录：{models_dir}\n"
            "排查建议：1) 确认该目录下已放入对应官方模型子目录；"
            "2) 离线环境请确认 PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True；"
            "3) 查看 logs/app.log 中的完整堆栈。"
        )
        low = msg.lower()
        if self.device_info.get("kind") == "gpu" and any(
            key in low
            for key in ("cuda", "cudnn", "cublas", "gpu", "driver", "device", "out of memory")
        ):
            hint += (
                "\nGPU 专项排查："
                "a) 宿主机 `nvidia-smi` 是否正常、驱动版本是否满足 paddlepaddle-gpu 要求；"
                "b) 容器需 `--gpus all` 或 compose 的 device reservations；"
                "c) 显存不足时调小 ocr.text_rec_batch_size / ocr.pdf_dpi，"
                "或用 ocr.gpu_mem_fraction 限制显存池；"
                "d) 想先用 CPU 跑通可把 ocr.device 改为 auto 或 cpu。"
            )
        return hint

    def ensure_loaded(self) -> None:
        """预加载一个实例（预热）。失败抛 EngineError。"""
        if self.status()["state"] == "ready":
            return
        started = time.time()
        self._set_status(state="loading", error=None)
        try:
            pipeline = self._new_pipeline()
        except EngineError as exc:
            self._set_status(state="error", error=str(exc), loaded=False)
            self.log.error("产线加载失败: %s", exc)
            raise
        self._slots.put(pipeline)
        self._set_status(
            state="ready",
            loaded=True,
            instances=self.status()["instances"] + 1,
            loaded_at=time.time(),
            load_seconds=round(time.time() - started, 2),
        )
        self.log.info(
            "产线已加载: %s [%s]（配置 %s）device=%s 耗时=%.1fs 模型目录=%s",
            self.label,
            self.pipeline_id,
            self.pipeline_arg,
            self.device,
            time.time() - started,
            self.settings.official_models_dir,
        )

    def _acquire(self) -> Any:
        slot = self._slots.get()
        if slot is not None:
            return slot
        with self._create_lock:
            pipeline = self._new_pipeline()
        self._set_status(instances=self.status()["instances"] + 1, state="ready", loaded=True)
        return pipeline

    def _release(self, pipeline: Any) -> None:
        self._slots.put(pipeline)

    # ------------------------------------------------------------------ 推理
    def _build_kwargs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        defaults: Dict[str, Any] = dict(self.settings.get("ocr.defaults", {}) or {})
        defaults.update({k: v for k, v in (params or {}).items() if v is not None})
        allowed = self.params_allowed
        kwargs: Dict[str, Any] = {}
        for key, value in defaults.items():
            if key not in allowed or value is None:
                continue
            base = (self.settings.get("ocr.defaults", {}) or {}).get(key)
            try:
                if isinstance(base, bool):
                    kwargs[key] = bool(value)
                elif isinstance(base, int) and not isinstance(base, bool):
                    kwargs[key] = int(value)
                elif isinstance(base, float):
                    kwargs[key] = float(value)
                else:
                    kwargs[key] = value
            except (TypeError, ValueError):
                continue
        # 纯 OCR 产线不支持表格参数
        if self.kind == "ocr":
            for key in ("use_table_recognition", "layout_threshold"):
                kwargs.pop(key, None)
        # 专用表格产线没有 parsing_res_list，结构类开关一律不传
        if self.kind == "table":
            for key in (
                "use_table_recognition",
                "use_formula_recognition",
                "use_seal_recognition",
                "use_chart_recognition",
                "use_general_ocr",
                "format_block_content",
                "layout_threshold",
                "layout_nms",
                "layout_unclip_ratio",
                "layout_merge_bboxes_mode",
            ):
                kwargs.pop(key, None)
        return kwargs

    def predict_page(self, image_path: Path, params: Dict[str, Any]) -> PageResult:
        kwargs = self._build_kwargs(params)
        pipeline = self._acquire()
        started = time.time()
        try:
            try:
                output = pipeline.predict(input=str(image_path), **kwargs)
            except TypeError:
                # 老版本不支持某些参数：退化到只传核心参数重试
                slim = {
                    k: v
                    for k, v in kwargs.items()
                    if k.startswith(("text_", "use_doc_", "use_textline_"))
                }
                self.log.warning("predict 参数不被当前 PaddleX 版本接受，改用精简参数重试：%s", slim)
                output = pipeline.predict(input=str(image_path), **slim)
            results = list(output)
        except Exception as exc:  # noqa: BLE001
            low = str(exc).lower()
            if any(k in low for k in ("out of memory", "resourceexhausted", "oom")):
                if self.device_info.get("kind") == "gpu":
                    raise EngineError(
                        "GPU 显存不足（CUDA out of memory）。可依次尝试："
                        "1) 调小 ocr.text_rec_batch_size（如 2~4）；"
                        "2) 调小 ocr.pdf_dpi / ocr.max_side_px；"
                        "3) 用 ocr.gpu_mem_fraction 限制显存池并重启；"
                        "4) 后台「服务控制 → 卸载」不用的产线；"
                        "5) 改用 CPU（ocr.device=cpu）先跑通。"
                        f"原始错误：{exc}"
                    ) from exc
                raise EngineError(
                    "内存不足（CPU）。请调小 ocr.pdf_dpi / ocr.max_side_px，"
                    "或降低 ocr.concurrency 后重试。"
                    f"原始错误：{exc}"
                ) from exc
            raise EngineError(f"推理失败：{exc}") from exc
        finally:
            self._release(pipeline)

        if not results:
            raise EngineError("推理返回空结果")
        raw_json = to_python(getattr(results[0], "json", results[0]))
        if not isinstance(raw_json, dict):
            raw_json = {"raw": raw_json}
        data = unwrap_result(raw_json)
        if self.kind == "ocr":
            blocks = blocks_from_ocr(data)
        elif self.kind == "table":
            blocks = blocks_from_table(data)
        else:
            blocks = blocks_from_structure(data)
        if not blocks:
            self.log.warning(
                "产线 %s 返回空结果，原始 JSON 顶层键=%s（已保存到 raw/ 便于排查）",
                self.pipeline_name,
                list(raw_json.keys())[:10],
            )
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        elapsed_ms = int((time.time() - started) * 1000)
        update: Dict[str, Any] = {
            "predict_count": self.status()["predict_count"] + 1,
            "last_predict_ms": elapsed_ms,
        }
        if self.device_info.get("kind") == "gpu":
            update["gpu_memory"] = device_mod.gpu_memory(
                int(self.device_info.get("index") or 0)
            )
        self._set_status(**update)
        return PageResult(
            blocks=blocks,
            width=width,
            height=height,
            raw=data if blocks else {"unwrapped": data, "original": raw_json},
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------ 诊断
    def model_inventory(self) -> Dict[str, Any]:
        # 文件遍历有开销，缓存 60 秒（健康检查会频繁调用）
        cached = getattr(self, "_inventory_cache", None)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        root = self.settings.official_models_dir
        models = []
        if root.exists():
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                    models.append({"name": child.name, "size_mb": round(size / 1024 / 1024, 1)})
        payload = {"dir": str(root), "exists": root.exists(), "models": models}
        self._inventory_cache = (time.time(), payload)
        return payload

    def close(self) -> None:
        while True:
            try:
                self._slots.get_nowait()
            except queue.Empty:
                break
        self._set_status(state="idle", loaded=False, instances=0)
