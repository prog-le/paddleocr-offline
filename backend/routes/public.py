"""公开接口：上传、任务查询、结果获取、导出、取消/删除。

无需登录（内网单站点工具），但管理员接口在 routes_admin.py 中单独鉴权。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .. import pdfio
from ..converters import FORMATS, ExportError, export_document
from ..docmodel import Doc
from .. import devices as device_mod
from .. import memguard
from ..engine import OCR_PARAMS, STRUCTURE_PARAMS
from ..manager import TaskError
from ..pipelines import PipelineSpec, find_spec, resolve_default_id
from ..storage import safe_filename

router = APIRouter()

ALLOWED_PARAM_RANGES: Dict[str, tuple] = {
    "layout_threshold": (0.0, 1.0),
    "text_det_thresh": (0.0, 1.0),
    "text_det_box_thresh": (0.0, 1.0),
    "text_det_unclip_ratio": (1.0, 4.0),
    "text_rec_score_thresh": (0.0, 1.0),
    "text_det_limit_side_len": (64, 4096),
}


def _sid(request: Request) -> str:
    """当前请求的匿名会话 id（由 main.py 的中间件注入）。"""
    return str(getattr(request.state, "sid", "") or "")


def _public_task(meta: Dict[str, Any]) -> Dict[str, Any]:
    """对外暴露的任务字段（隐藏内部路径/堆栈）。"""
    keys = (
        "id", "batch_id", "filename", "size", "source_type", "status", "progress", "stage",
        "pages_total", "pages_done", "created_at", "started_at", "finished_at", "duration_sec",
        "error", "pipeline", "queue_position", "running_count",
    )
    out = {k: meta.get(k) for k in keys if k in meta}
    out["stats"] = meta.get("stats")
    out["pipeline"] = meta.get("pipeline")
    out["pipeline_label"] = meta.get("pipeline_label") or meta.get("pipeline")
    out["params"] = meta.get("params") or {}
    out["result_available"] = meta.get("status") == "success"
    return out


def _sanitize_params(raw: Dict[str, Any], spec: PipelineSpec) -> Dict[str, Any]:
    allowed = set(spec.params) if spec is not None else STRUCTURE_PARAMS
    clean: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)):
            low, high = ALLOWED_PARAM_RANGES.get(key, (None, None))
            v = float(value)
            if low is not None and high is not None:
                v = min(max(v, low), high)
            clean[key] = int(v) if key == "text_det_limit_side_len" else v
        elif isinstance(value, str) and key in ("text_det_limit_type", "layout_merge_bboxes_mode"):
            if value in ("min", "max", "large", "small", "union"):
                clean[key] = value
    return clean


async def _save_upload(upload: UploadFile, dest: Path, limit: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小上限 {limit // 1024 // 1024} MB",
                    )
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存上传文件失败：{exc}") from exc
    return total


@router.post("/api/tasks")
async def create_tasks(
    request: Request,
    files: List[UploadFile] = File(..., description="待识别文件，支持多选"),
    params: str = Form("{}", description="识别参数 JSON"),
    pipeline: str = Form("", description="产线 id（留空用默认产线）"),
) -> JSONResponse:
    settings = request.app.state.settings
    manager = request.app.state.manager
    specs = request.app.state.pipeline_specs

    files = [f for f in files if f and f.filename]
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    max_files = int(settings.get("limits.max_files_per_batch", 20) or 20)
    if len(files) > max_files:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {max_files} 个文件")

    try:
        raw_params = json.loads(params or "{}")
        if not isinstance(raw_params, dict):
            raw_params = {}
    except ValueError:
        raise HTTPException(status_code=400, detail="params 必须是合法 JSON") from None

    # 产线选择：留空用默认；填了但不存在则明确报错（列出可选项）
    if pipeline:
        spec = find_spec(specs, pipeline)
        if spec is None:
            available = "、".join(f"{sp.id}（{sp.label}）" for sp in specs)
            raise HTTPException(status_code=400, detail=f"未知产线：{pipeline}。可选：{available}")
    else:
        spec = find_spec(specs, manager.default_pipeline) or (specs[0] if specs else None)
    if spec is None:
        raise HTTPException(status_code=503, detail="服务未配置任何可用产线")

    clean_params = _sanitize_params(raw_params, spec)
    limit = settings.max_upload_bytes
    staging = settings.data_dir / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    batch_id = f"b{time.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"

    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for upload in files:
        original = safe_filename(upload.filename, "upload")
        suffix = Path(original).suffix.lower()
        if suffix not in pdfio.SUPPORTED_SUFFIX:
            errors.append({"filename": original, "error": f"不支持的文件类型 {suffix or '(无)'}", "code": 400})
            continue
        staged = staging / f"{uuid.uuid4().hex}{suffix}"
        try:
            size = await _save_upload(upload, staged, limit)
        except HTTPException as exc:
            errors.append({"filename": original, "error": str(exc.detail), "code": exc.status_code})
            continue
        if size == 0:
            staged.unlink(missing_ok=True)
            errors.append({"filename": original, "error": "文件为空", "code": 400})
            continue
        kind = pdfio.sniff_kind(staged)
        if kind is None:
            staged.unlink(missing_ok=True)
            errors.append({"filename": original, "error": "文件内容无法识别（不是有效的图片或 PDF）", "code": 400})
            continue
        try:
            meta = manager.submit(
                input_path=staged,
                filename=original,
                size=size,
                params=clean_params,
                batch_id=batch_id,
                source_type=kind,
                owner=_sid(request),
                pipeline=spec.id,
                pipeline_label=spec.label,
            )
            created.append(_public_task(meta))
        except TaskError as exc:
            staged.unlink(missing_ok=True)
            errors.append({"filename": original, "error": str(exc), "code": 429 if "队列" in str(exc) else 400})

    if not created:
        # 全部失败时按最贴近的语义返回状态码：体积超限 -> 413，队列满 -> 429，其余 400
        codes = {int(e.get("code") or 400) for e in errors}
        if codes == {413}:
            status = 413
        elif codes == {429}:
            status = 429
        else:
            status = 400
        raise HTTPException(
            status_code=status,
            detail="；".join(f"{e['filename']}: {e['error']}" for e in errors) or "提交失败",
        )
    return JSONResponse({"batch_id": batch_id, "tasks": created, "errors": errors})


@router.get("/api/tasks")
async def list_tasks(
    request: Request,
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: Optional[str] = Query(None),
) -> Dict[str, Any]:
    manager = request.app.state.manager
    sid = _sid(request)
    data = manager.list_tasks(status=status, limit=limit, offset=offset, keyword=keyword, owner=sid)
    return {
        "total": data["total"],
        "items": [_public_task(m) for m in data["items"]],
        # 只返回本会话的统计，避免暴露其他用户的任务量
        "queue": manager.stats(owner=sid),
    }


@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> Dict[str, Any]:
    manager = request.app.state.manager
    meta = manager.get_owned(task_id, _sid(request))
    if not meta:
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    return _public_task(meta)


@router.get("/api/tasks/{task_id}/result")
async def get_result(request: Request, task_id: str) -> Dict[str, Any]:
    manager = request.app.state.manager
    meta = manager.get_owned(task_id, _sid(request))
    if not meta:
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    if meta.get("status") != "success":
        raise HTTPException(status_code=409, detail=f"任务尚未完成（当前状态：{meta.get('status')}）")
    result = manager.result(task_id)
    if not result:
        raise HTTPException(status_code=410, detail="结果文件已被清理，请重新识别")
    return result


@router.get("/api/tasks/{task_id}/text")
async def get_plain_text(request: Request, task_id: str) -> PlainTextResponse:
    """一键复制用的纯文本。"""
    manager = request.app.state.manager
    if not manager.get_owned(task_id, _sid(request)):
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    result = manager.result(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="结果不存在")
    from ..converters.to_txt import build_txt

    doc = Doc.from_dict(result)
    noise = bool(request.app.state.settings.get("export.include_noise_blocks", False))
    return PlainTextResponse(
        build_txt(doc, include_noise=noise, page_markers=False), media_type="text/plain; charset=utf-8"
    )


@router.get("/api/tasks/{task_id}/export")
async def export(
    request: Request,
    task_id: str,
    fmt: str = Query("docx", pattern="^(docx|xlsx|txt|md)$"),
    noise: int = Query(0, ge=0, le=1),
):
    settings = request.app.state.settings
    manager = request.app.state.manager
    meta = manager.get_owned(task_id, _sid(request))
    if not meta:
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    if meta.get("status") != "success":
        raise HTTPException(status_code=409, detail="任务尚未完成，无法导出")
    result = manager.result(task_id)
    if not result:
        raise HTTPException(status_code=410, detail="结果文件已被清理，请重新识别")

    doc = Doc.from_dict(result)
    ext = FORMATS[fmt]["ext"]
    base = Path(str(meta.get("filename") or "result")).stem or "result"
    out_dir = settings.exports_dir / task_id
    out_path = out_dir / f"{safe_filename(base, 'result')}{ext}"

    # 同一参数的导出结果直接复用（避免重复转换大文档）
    stamp_key = out_dir / f".{fmt}.{int(noise)}.stamp"
    if not out_path.exists() or not stamp_key.exists():
        try:
            export_document(
                doc,
                fmt,
                out_path,
                include_noise=bool(noise) or bool(settings.get("export.include_noise_blocks", False)),
                txt_page_markers=bool(settings.get("export.txt_page_markers", True)),
                docx_page_break=bool(settings.get("export.docx_page_break", True)),
            )
            stamp_key.write_text(str(time.time()), encoding="utf-8")
        except ExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"生成 {fmt} 失败：{exc}") from exc

    filename = f"{base}{ext}"
    ascii_fallback = safe_filename(filename).encode("ascii", "ignore").decode() or f"result{ext}"
    disposition = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return FileResponse(
        path=str(out_path),
        media_type=FORMATS[fmt]["media"],
        headers={"Content-Disposition": disposition},
    )


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str) -> Dict[str, Any]:
    manager = request.app.state.manager
    if not manager.get_owned(task_id, _sid(request)):
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    try:
        meta = manager.cancel(task_id)
    except TaskError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _public_task(meta)


@router.delete("/api/tasks/{task_id}")
async def delete_task(request: Request, task_id: str) -> Dict[str, Any]:
    manager = request.app.state.manager
    if not manager.get_owned(task_id, _sid(request)):
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    try:
        ok = manager.delete(task_id)
    except TaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"deleted": True, "id": task_id}


@router.get("/api/meta")
async def meta(request: Request) -> Dict[str, Any]:
    settings = request.app.state.settings
    manager = request.app.state.manager
    data = settings.public_dict()
    data["formats"] = {k: {"label": v["label"], "ext": v["ext"]} for k, v in FORMATS.items()}
    data["supported_ext"] = sorted(pdfio.SUPPORTED_SUFFIX)
    specs = request.app.state.pipeline_specs
    data["pipelines"] = [sp.to_dict() for sp in specs]
    data["pipeline"] = manager.default_pipeline
    # 智能产线选择能力：后端 smart_router 里的 select_pipeline 会先探测文件类型 + PDF 分类再选产线
    data["auto_pipeline"] = bool(settings.get("ocr.auto_pipeline", True))
    data["auto_pipeline_label"] = "⚡ 智能选择（推荐）"
    data["mock"] = bool(getattr(request.app.state.engine, "is_mock", False))
    data["pdf_backend"] = _safe_pdf_backend()
    data["session"] = {"id": _sid(request)[:8], "isolated": True}
    # 运行设备：前端展示"CPU / GPU0 · 型号"，并提示 macOS 只能 CPU
    data["device"] = device_mod.summary(settings)
    data["param_ranges"] = {k: list(v) for k, v in ALLOWED_PARAM_RANGES.items()}
    return data


def _safe_pdf_backend() -> str:
    try:
        return pdfio.pdf_backend_name()
    except Exception:  # noqa: BLE001
        return "none"


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    engine = request.app.state.engine
    manager = request.app.state.manager
    engine_status = engine.status()
    models = engine.model_inventory()
    stats = manager.stats()
    ok = True
    issues: List[str] = []
    if engine_status.get("state") == "error":
        ok = False
        issues.append(str(engine_status.get("error") or "产线加载失败"))
    if not models.get("exists") and not getattr(engine, "is_mock", False):
        ok = False
        issues.append(f"模型目录不存在：{models.get('dir')}")
    elif models.get("exists") and not models.get("models") and not getattr(engine, "is_mock", False):
        ok = False
        issues.append("模型目录为空，请执行 scripts/download_models.py 打包模型")
    # 设备：配置了 GPU 但不可用时明确报出来（否则用户只看到"慢"）
    dev = device_mod.summary(settings)
    active = dev.get("active") or {}
    if active.get("fallback") and active.get("reason"):
        ok = False
        issues.append(str(active["reason"]))
    if active.get("kind") == "gpu":
        gpu_mem = dev.get("memory") or {}
        if gpu_mem.get("free_mem_mb") is not None and gpu_mem["free_mem_mb"] < 800:
            ok = False
            issues.append(
                f"GPU 显存仅剩 {gpu_mem['free_mem_mb']}MB"
                f"（整卡 {gpu_mem.get('used_mem_mb', '?')}/{gpu_mem.get('total_mem_mb', '?')}MB），"
                "请调小 ocr.text_rec_batch_size / pdf_dpi，或卸载不用的产线"
            )

    # 内存：容器里给出 cgroup 限额与余量，便于发现“快被 OOM kill 了”
    mem = memguard.snapshot()
    if mem.get("limit_mb") and mem.get("free_mb") is not None:
        if mem["free_mb"] < 400:
            ok = False
            issues.append(
                f"容器可用内存仅 {mem['free_mb']:.0f}MB（上限 {mem['limit_mb']:.0f}MB），"
                "识别可能被 OOM kill；请调大 MEM_LIMIT 或降低 pdf_dpi / max_side_px"
            )

    body = {
        "status": "ok" if ok else "degraded",
        "issues": issues,
        "memory": mem,
        "device": dev,
        "pipeline": manager.default_pipeline,
        "pipelines": [sp.to_dict() for sp in request.app.state.pipeline_specs],
        "engine": engine_status,
        "models": {"dir": models.get("dir"), "count": len(models.get("models") or [])},
        "queue": {"pending": stats["pending"], "running": stats["running"], "paused": stats["paused"]},
        "version": settings.get("version", "1.0.0"),
        "time": time.time(),
    }
    return JSONResponse(body, status_code=200 if ok else 503)
