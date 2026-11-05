"""产线实例池。

一个站点同时支持多条 PaddleX 产线（通用版面解析 v3 完整版/精简版、纯 OCR…）：
  * 每个产线按需懒加载（首次用到才加载模型），互不影响；
  * 任务在提交时记录所选产线 id，推理时路由到对应实例；
  * 状态/模型清单对上层统一暴露，管理员可看到每条产线的加载情况。

内存提示：每条产线各自持有一套模型，同时加载两条会翻倍占用（精简 1.9G / 完整 2.5G），
`ocr.concurrency` 仍作用于每条产线内部的实例数。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional

from . import devices as device_mod
from .logging_setup import get_logger
from .pipelines import PipelineSpec, find_spec, resolve_default_id
from .settings import Settings


class EnginePool:
    def __init__(self, settings: Settings, specs: List[PipelineSpec]):
        self.settings = settings
        self.specs = specs
        self.default_id = resolve_default_id(settings, specs)
        self.is_mock = os.environ.get("PADDLEX_OCR_MOCK", "").lower() in ("1", "true", "yes", "on")
        self.log = get_logger("ocr.engines")
        self._engines: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._mock_cls = None
        self._real_cls = None
        # 设备解析结果（用于空闲产线的状态展示；真实设备以各引擎为准）
        try:
            self.device_info = device_mod.resolve(
                str(settings.get("ocr.device", "auto") or "auto")
            ).to_dict()
        except device_mod.DeviceError as exc:
            self.device_info = {
                "requested": str(settings.get("ocr.device", "auto")),
                "device": "cpu",
                "kind": "cpu",
                "fallback": True,
                "reason": str(exc),
                "warnings": [],
            }
            self.log.error("设备配置不可用：%s", exc)

    # ------------------------------------------------------------------ 实例
    def _new_engine(self, spec: PipelineSpec) -> Any:
        if self.is_mock:
            if self._mock_cls is None:
                from .mock_engine import MockEngine

                self._mock_cls = MockEngine
            return self._mock_cls(self.settings, spec)
        if getattr(spec, "backend", "paddlex") == "indra":
            from .indra_engine import IndraEngine

            self.log.info("使用 Indra OCR 后端：%s", spec.id)
            return IndraEngine(self.settings, spec)
        if getattr(spec, "backend", "paddlex") == "pdf_inspector":
            from .pdf_inspector_engine import PdfInspectorEngine

            self.log.info("使用 pdf-inspector 后端：%s", spec.id)
            return PdfInspectorEngine(self.settings, spec)
        if self._real_cls is None:
            from .engine import PaddleXEngine

            self._real_cls = PaddleXEngine
        return self._real_cls(self.settings, spec)

    def get(self, pipeline_id: Optional[str] = None) -> Any:
        pid = pipeline_id or self.default_id
        spec = find_spec(self.specs, pid)
        if spec is None:
            spec = find_spec(self.specs, self.default_id) or (self.specs[0] if self.specs else None)
            if spec is None:
                raise RuntimeError("没有可用的识别产线")
            pid = spec.id
        with self._lock:
            engine = self._engines.get(pid)
            if engine is None:
                engine = self._new_engine(spec)
                self._engines[pid] = engine
                self.log.info("按需创建产线实例：%s（%s）", spec.label, pid)
            return engine

    def ensure_loaded(self, pipeline_id: Optional[str] = None) -> None:
        self.get(pipeline_id).ensure_loaded()

    @property
    def default_engine(self) -> Any:
        return self.get(self.default_id)

    @property
    def pipeline_name(self) -> str:
        """兼容旧字段：默认产线 id。"""
        return self.default_id

    # ------------------------------------------------------------------ 状态
    def _idle_stub(self, spec: PipelineSpec) -> Dict[str, Any]:
        return {
            "state": "idle",
            "pipeline": spec.id,
            "label": spec.label,
            "device": self.device_info.get("device", "cpu"),
            "device_requested": self.device_info.get("requested"),
            "device_info": self.device_info,
            "loaded": False,
            "instances": 0,
            "error": None,
            "loaded_at": None,
            "load_seconds": None,
            "predict_count": 0,
            "last_predict_ms": None,
        }

    def status(self) -> Dict[str, Any]:
        detail: Dict[str, Any] = {}
        for spec in self.specs:
            engine = self._engines.get(spec.id)
            detail[spec.id] = engine.status() if engine else self._idle_stub(spec)

        default_status = dict(detail.get(self.default_id) or self._idle_stub(self.specs[0]))
        default_status["pipelines"] = detail
        default_status["default_pipeline"] = self.default_id
        default_status["mock"] = self.is_mock
        default_status["device_info"] = self.device_info
        default_status["loaded_pipelines"] = [k for k, v in detail.items() if v.get("loaded")]
        # 任一产线出错都向上暴露，便于后台一键看出问题
        errors = [f"{k}: {v.get('error')}" for k, v in detail.items() if v.get("state") == "error"]
        default_status["errors"] = errors
        return default_status

    def model_inventory(self) -> Dict[str, Any]:
        return self.default_engine.model_inventory()

    def reset(self, pipeline_id: Optional[str] = None) -> None:
        """丢弃实例，下次 get() 会按**当前配置**重新创建。

        用途：后台改了 ocr.device / ocr.ocr_tier / 批大小后，这些值在引擎构造或建模时
        才生效，必须重建实例而不能只 close（close 只是清空实例池）。
        """
        with self._lock:
            if pipeline_id:
                targets = [self._engines.pop(pipeline_id, None)]
            else:
                targets = list(self._engines.values())
                self._engines.clear()
        for engine in targets:
            if engine is None:
                continue
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.device_info = device_mod.resolve(
                str(self.settings.get("ocr.device", "auto") or "auto")
            ).to_dict()
        except device_mod.DeviceError as exc:
            self.device_info = {"device": "cpu", "kind": "cpu", "fallback": True, "reason": str(exc)}
        self.log.info("已重置产线实例：%s", pipeline_id or "全部")

    def close(self, pipeline_id: Optional[str] = None) -> None:
        with self._lock:
            if pipeline_id:
                engine = self._engines.pop(pipeline_id, None)
                targets = [engine] if engine else []
            else:
                targets = list(self._engines.values())
                self._engines.clear()
        for engine in targets:
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                pass
