"""管理后台接口：需登录（Cookie 会话）。

覆盖需求中的「查看/控制服务状态、任务状态、任务队列、统计数据」：
  * 服务状态：产线加载状态、模型清单、CPU/内存/磁盘、运行时长
  * 任务：全量列表（含错误详情）、取消、重试、删除、单任务日志
  * 队列：排队数、运行数、暂停/恢复接单
  * 统计：总量、成功/失败、平均耗时、总页数、今日任务
  * 运维：查看 app.log、触发过期清理、重新加载模型、在线修改部分配置
"""

from __future__ import annotations

import os
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from .. import devices as device_mod
from .. import memguard
from ..manager import TaskError
from ..security import COOKIE_NAME, AuthError
from ..settings import PROJECT_ROOT, RUNTIME_EDITABLE

router = APIRouter(prefix="/api/admin")


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


def _auth(request: Request):
    return request.app.state.auth


def require_admin(request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if not _auth(request).validate(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")


@router.post("/login")
async def login(request: Request, payload: LoginPayload, response: Response) -> Dict[str, Any]:
    client = request.client.host if request.client else "-"
    try:
        token = _auth(request).login(payload.username, payload.password, client)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_auth(request).cookie_max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "username": payload.username}


@router.post("/logout")
async def logout(request: Request, response: Response) -> Dict[str, Any]:
    _auth(request).destroy(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/session")
async def session(request: Request) -> Dict[str, Any]:
    ok = _auth(request).validate(request.cookies.get(COOKIE_NAME))
    if not ok:
        raise HTTPException(status_code=401, detail="未登录")
    return {"ok": True}


def _system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "arch": platform.machine(),
        "platform": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }
    try:
        import psutil

        vm = psutil.virtual_memory()
        du = psutil.disk_usage(str(PROJECT_ROOT))
        info.update(
            {
                "cpu_count": psutil.cpu_count(logical=True),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "mem_total_mb": round(vm.total / 1024 / 1024),
                "mem_used_mb": round((vm.total - vm.available) / 1024 / 1024),
                "mem_percent": vm.percent,
                "disk_total_gb": round(du.total / 1024 ** 3, 1),
                "disk_free_gb": round(du.free / 1024 ** 3, 1),
            }
        )
        try:
            info["load_avg"] = [round(v, 2) for v in os.getloadavg()]
        except OSError:
            pass
        proc = psutil.Process()
        info["proc_rss_mb"] = round(proc.memory_info().rss / 1024 / 1024)
        info["proc_threads"] = proc.num_threads()
        # 容器口径：cgroup 限额与余量（docker 里 mem_total_mb 是宿主机内存，容易误判）
        mem = memguard.snapshot()
        if mem.get("limit_mb"):
            info["container_mem_limit_mb"] = mem["limit_mb"]
            info["container_mem_used_mb"] = mem.get("used_mb")
            info["container_mem_free_mb"] = mem.get("free_mb")
            info["container_mem_source"] = mem.get("source")
    except Exception:  # noqa: BLE001 - psutil 缺失不影响后台其他功能
        pass
    return info


@router.get("/overview")
async def overview(request: Request) -> Dict[str, Any]:
    require_admin(request)
    settings = request.app.state.settings
    manager = request.app.state.manager
    engine = request.app.state.engine
    stats = manager.stats()
    models = engine.model_inventory()
    return {
        "service": {
            "uptime_sec": stats["uptime_sec"],
            "started_at": time.time() - stats["uptime_sec"],
            "version": settings.get("version", "1.0.0"),
            "paused": stats["paused"],
            "mock": bool(getattr(engine, "is_mock", False)),
            "pipeline": manager.default_pipeline,
            "device_summary": device_mod.summary(settings),
            "pipelines": [sp.to_dict() for sp in request.app.state.pipeline_specs],
            "device": settings.get("ocr.device", "cpu"),
            "concurrency": stats["concurrency"],
        },
        "engine": engine.status(),
        "models": models,
        "tasks": stats,
        "system": _system_info(),
        "storage": {**request.app.state.storage.disk_usage(), "data_dir": str(settings.data_dir)},
        "paths": {
            "project": str(PROJECT_ROOT),
            "data": str(settings.data_dir),
            "logs": str(settings.log_dir),
            "models": str(settings.official_models_dir),
        },
    }


@router.get("/tasks")
async def admin_tasks(
    request: Request,
    status: str = Query("all"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query(""),
) -> Dict[str, Any]:
    require_admin(request)
    manager = request.app.state.manager
    data = manager.list_tasks(status=status, limit=limit, offset=offset, keyword=keyword or None)
    items: List[Dict[str, Any]] = []
    from ..session import ClientSessions

    for meta in data["items"]:
        item = dict(meta)
        item.pop("stored_name", None)
        # 给管理员展示"来自哪个浏览器会话"（完整 sid 不返回）
        item["session"] = ClientSessions.short(str(meta.get("owner") or ""))
        item.pop("owner", None)
        items.append(item)
    return {"total": data["total"], "items": items, "queue": manager.stats()}


@router.post("/tasks/{task_id}/cancel")
async def admin_cancel(request: Request, task_id: str) -> Dict[str, Any]:
    require_admin(request)
    try:
        return request.app.state.manager.cancel(task_id)
    except TaskError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/retry")
async def admin_retry(request: Request, task_id: str) -> Dict[str, Any]:
    require_admin(request)
    try:
        return request.app.state.manager.retry(task_id)
    except TaskError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/tasks/{task_id}")
async def admin_delete(request: Request, task_id: str) -> Dict[str, Any]:
    require_admin(request)
    try:
        ok = request.app.state.manager.delete(task_id)
    except TaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"deleted": True}


@router.get("/tasks/{task_id}/log")
async def task_log(request: Request, task_id: str, lines: int = Query(300, ge=10, le=5000)) -> Dict[str, Any]:
    require_admin(request)
    manager = request.app.state.manager
    if not manager.get(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"id": task_id, "content": manager.task_log_tail(task_id, lines)}


@router.get("/logs")
async def app_logs(
    request: Request,
    file: str = Query("app.log"),
    lines: int = Query(300, ge=10, le=5000),
) -> Dict[str, Any]:
    require_admin(request)
    settings = request.app.state.settings
    log_dir = Path(settings.log_dir)
    files = sorted(p.name for p in log_dir.glob("*.log*") if p.is_file())
    target = log_dir / os.path.basename(file)
    content = ""
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = "".join(f.readlines()[-lines:])
        except OSError as exc:
            content = f"读取失败：{exc}"
    return {"files": files, "file": target.name, "content": content, "dir": str(log_dir)}


@router.get("/config")
async def get_config(request: Request) -> Dict[str, Any]:
    require_admin(request)
    settings = request.app.state.settings
    return {
        "config": settings.admin_dict(),
        "config_path": str(settings.config_path),
        "editable": list(RUNTIME_EDITABLE),
    }


@router.patch("/config")
async def patch_config(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    require_admin(request)
    settings = request.app.state.settings
    try:
        applied = settings.update_runtime(payload or {})
    except ValueError as exc:
        # 设备/档位/批大小等枚举值非法：直接 400，避免写入不可用配置
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 这些键在引擎构造/建模时生效，必须重载产线；提示前端引导用户点「重载」
    hot_keys = ("device", "ocr_tier", "text_rec_batch_size", "textline_batch_size", "gpu_mem_limit_mb")
    requires_reload = any(
        (k in (applied.get("ocr") or {})) for k in hot_keys
    )
    return {
        "ok": True,
        "applied": applied,
        "requires_reload": requires_reload,
        "config": settings.admin_dict(),
    }


class ActionPayload(BaseModel):
    action: str = ""


@router.post("/service/{action}")
async def service_action(
    request: Request, action: str, pipeline: str = Query("", description="产线 id，留空为默认产线")
) -> Dict[str, Any]:
    require_admin(request)
    manager = request.app.state.manager
    engines = request.app.state.engines
    target = pipeline or manager.default_pipeline
    if action == "pause":
        manager.pause()
        return {"ok": True, "paused": True}
    if action == "resume":
        manager.resume()
        return {"ok": True, "paused": False}
    if action == "warmup":
        try:
            engines.ensure_loaded(target)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "pipeline": target, "engine": engines.status()}
    if action == "reload":
        try:
            # reset 会丢弃实例，使设备/档位/批大小等构造期配置重新生效。
            # 不指定 pipeline 时重置**全部**产线：设备是全局配置，只重载一条会留下
            # 旧设备上的其它实例，导致同一进程既有 GPU 又有 CPU 实例。
            if pipeline:
                engines.reset(pipeline)
                engines.ensure_loaded(pipeline)
            else:
                engines.reset()
                engines.ensure_loaded()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "pipeline": target, "reloaded_all": not bool(pipeline),
                "engine": engines.status()}
    if action == "unload":
        engines.close(target)
        return {"ok": True, "pipeline": target, "engine": engines.status()}
    raise HTTPException(status_code=400, detail=f"未知操作：{action}")


@router.post("/maintenance/cleanup")
async def cleanup(request: Request) -> Dict[str, Any]:
    require_admin(request)
    removed = request.app.state.manager.cleanup_expired()
    return {"ok": True, "removed": removed, "count": len(removed)}


@router.get("/health")
async def admin_health(request: Request) -> Dict[str, Any]:
    require_admin(request)
    return {"ok": True, "time": time.time()}
