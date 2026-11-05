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
from ..engine import STRUCTURE_PARAMS
from ..manager import TaskError
from ..pipelines import PipelineSpec, find_spec
from ..smart_router import AUTO_TOKENS
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
    # 智能产线选择的理由（smart_router 写入）
    out["pipeline_auto_reason"] = meta.get("pipeline_auto_reason")
    out["params"] = meta.get("params") or {}
    out["result_available"] = meta.get("status") == "success"
    return out


def _sanitize_params(
    raw: Dict[str, Any],
    spec: Optional[PipelineSpec] = None,
    specs: Optional[List[PipelineSpec]] = None,
) -> Dict[str, Any]:
    """只保留指定产线支持的参数，并夹紧阈值范围。

    智能路由（spec 未知）时按所有可用产线的参数并集白名单过滤，
    避免未经校验的参数直达引擎。
    """
    if spec is not None:
        allowed = set(spec.params)
    elif specs:
        allowed = set().union(*(set(sp.params) for sp in specs))
    else:
        allowed = set(STRUCTURE_PARAMS)
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


def _validate_pipeline(specs: List[PipelineSpec], pipeline: Optional[str]) -> None:
    """自动路由 token 放行；显式产线 id 必须存在，否则 400。"""
    if pipeline is None or not pipeline or pipeline in AUTO_TOKENS:
        return
    if find_spec(specs, pipeline) is None:
        available = "、".join(f"{sp.id}（{sp.label}）" for sp in specs)
        raise HTTPException(
            status_code=400, detail=f"未知产线：{pipeline}。可选：{available}"
        )


async def _parse_retry_body(request: Request) -> Dict[str, Any]:
    """读取重试接口的可选 JSON body（空 body / 非法 body 视为无覆盖）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 无 body / 非 JSON
        return {}
    return body if isinstance(body, dict) else {}


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
    pipeline: str = Form("", description="产线 id；留空 / auto = 智能选择"),
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

    # 产线选择：
    #   - 空字符串 / auto-tokens → spec=None，manager.run 里对每个文件独立智能路由
    #   - 有效 id → 强制该产线（所有上传文件共用同一条）
    #   - 无效 id → 报错列出可选项
    pipeline_chosen_by_user = bool(pipeline) and pipeline not in AUTO_TOKENS
    spec: Optional[PipelineSpec] = None
    if pipeline_chosen_by_user:
        spec = find_spec(specs, pipeline)
        if spec is None:
            available = "、".join(f"{sp.id}（{sp.label}）" for sp in specs)
            raise HTTPException(status_code=400, detail=f"未知产线：{pipeline}。可选：{available}")
    elif not settings.get("ocr.auto_pipeline", True):
        # 后端关闭了智能选择 → 固定用默认产线
        spec = find_spec(specs, manager.default_pipeline) or (specs[0] if specs else None)
    if spec is None and not pipeline_chosen_by_user and not specs:
        raise HTTPException(status_code=503, detail="服务未配置任何可用产线")

    # 智能路由模式下按全部产线的参数并集过滤，避免未校验参数直达引擎
    clean_params = _sanitize_params(raw_params, spec=spec, specs=specs if spec is None else None)
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
        if spec is not None and spec.kind == "pdf_inspector" and kind != "pdf":
            staged.unlink(missing_ok=True)
            errors.append({
                "filename": original,
                "error": "PDFINSPECTOR 产线只支持 PDF 文件；图片请选择 PaddleX 产线或「智能选择」",
                "code": 400,
            })
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
                # pipeline=None → smart_router 按文件类型自动选择
                pipeline=spec.id if spec else None,
                pipeline_label=spec.label if spec else "⚡ 智能选择",
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
    status: str = Query("all", pattern="^(all|active|pending|running|success|failed|cancelled)$"),
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
        status = (manager.get(task_id) or {}).get("status")
        if status != "success":
            raise HTTPException(status_code=409, detail=f"任务尚未完成（当前状态：{status}）")
        raise HTTPException(status_code=410, detail="结果文件已被清理，请重新识别")
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

    # 导出缓存：同一份结果 + 同一组导出选项才复用（避免把 noise=1 的文件当成 noise=0 返回）
    include_noise = bool(noise) or bool(settings.get("export.include_noise_blocks", False))
    txt_page_markers = bool(settings.get("export.txt_page_markers", True))
    docx_page_break = bool(settings.get("export.docx_page_break", True))
    cache_key = f"{fmt}|{int(noise)}|{int(include_noise)}|{int(txt_page_markers)}|{int(docx_page_break)}"
    stamp_key = out_dir / ".stamp"
    try:
        cached_key = stamp_key.read_text(encoding="utf-8").strip() if stamp_key.exists() else ""
    except OSError:
        cached_key = ""
    if not out_path.exists() or cached_key != cache_key:
        try:
            export_document(
                doc,
                fmt,
                out_path,
                include_noise=include_noise,
                txt_page_markers=txt_page_markers,
                docx_page_break=docx_page_break,
            )
            stamp_key.write_text(cache_key, encoding="utf-8")
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


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(request: Request, task_id: str) -> Dict[str, Any]:
    """重试自己的任务（owner 权限）。可选 JSON body: {"pipeline": "...", "params": {...}}

    - pipeline="" / "auto" / 空  → 重新触发智能产线选择
    - pipeline="PP-StructureV3"  → 强制换产线
    - params 只覆盖传入的字段
    """
    manager = request.app.state.manager
    if not manager.get_owned(task_id, _sid(request)):
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前会话")
    body = await _parse_retry_body(request)
    pipeline = body.get("pipeline") if "pipeline" in body else None
    if pipeline is not None:
        pipeline = str(pipeline or "")
        _validate_pipeline(request.app.state.pipeline_specs, pipeline)
    params = body.get("params") if isinstance(body.get("params"), dict) else None
    if params is not None:
        target = find_spec(request.app.state.pipeline_specs, pipeline) if pipeline else None
        params = _sanitize_params(
            params,
            spec=target,
            specs=request.app.state.pipeline_specs if target is None else None,
        )
    try:
        meta = manager.retry(task_id, pipeline=pipeline, params=params)
    except TaskError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _public_task(meta)


@router.post("/api/batches/{batch_id}/retry")
async def retry_batch(request: Request, batch_id: str) -> Dict[str, Any]:
    """批量重跑同一 batch 中属于当前会话的任务（owner 权限）。

    运行中/排队中的自动跳过。可选 JSON body 同上：
    {"pipeline": "", "params": {...}}  → 全批统一换产线 / 改参数
    """
    manager = request.app.state.manager
    specs = request.app.state.pipeline_specs
    body = await _parse_retry_body(request)
    pipeline = body.get("pipeline") if "pipeline" in body else None
    if pipeline is not None:
        pipeline = str(pipeline or "")
        _validate_pipeline(specs, pipeline)
    params = body.get("params") if isinstance(body.get("params"), dict) else None
    if params is not None:
        target = find_spec(specs, pipeline) if pipeline else None
        params = _sanitize_params(params, spec=target, specs=specs if target is None else None)
    try:
        result = manager.retry_batch(batch_id, owner=_sid(request), pipeline=pipeline, params=params)
    except TaskError as exc:
        raise HTTPException(status_code=404 if "不属于" in str(exc) else 400, detail=str(exc)) from exc
    return result


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
