"""PaddleX OCR Web —— FastAPI 应用入口。

启动顺序（很重要）：
  1. 读取配置 -> 2. 写入 PaddleX 离线环境变量 -> 3. 初始化日志 ->
  4. 创建推理引擎（真实或 mock）-> 5. 任务管理器 -> 6. 挂载路由与前端静态资源
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .engine_pool import EnginePool
from .logging_setup import get_logger, setup_logging
from .manager import TaskManager
from .pipelines import build_registry
from .routes import admin as admin_routes
from .routes import public as public_routes
from .security import AdminAuth
from .session import ClientSessions
from .settings import PROJECT_ROOT, Settings, apply_paddlex_env, get_settings
from .storage import Storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = get_logger("ocr.app")
    log.info("=" * 70)
    log.info("PaddleX OCR Web %s 启动中…", __version__)
    log.info("项目目录: %s", PROJECT_ROOT)
    log.info("数据目录: %s", app.state.settings.data_dir)
    log.info("日志目录: %s", app.state.settings.log_dir)
    log.info("模型目录: %s", app.state.settings.official_models_dir)
    try:
        from . import devices as _devices

        log.info("运行设备: %s（配置 %s）", _devices.describe(app.state.settings),
                 app.state.settings.get("ocr.device"))
    except Exception:  # noqa: BLE001
        pass
    log.info("默认产线: %s 可用: %s 并发: %s mock=%s",
             app.state.manager.default_pipeline,
             ",".join(sp.id for sp in app.state.pipeline_specs),
             app.state.settings.get("ocr.concurrency"),
             getattr(app.state.engines, "is_mock", False))
    log.info("监听: http://%s:%s", app.state.settings.get("server.host"), app.state.settings.get("server.port"))
    if str(app.state.settings.get("server.admin_password", "")) in ("PaddleX@2026", "admin", "123456", ""):
        log.warning(
            "⚠ 管理员口令仍是默认值，请修改 config/config.yaml 的 server.admin_password "
            "或设置环境变量 PADDLEX_OCR_ADMIN_PASSWORD 后重启"
        )
    yield
    log.info("正在关闭服务…")
    app.state.manager.shutdown()
    log.info("服务已停止")


def create_app() -> FastAPI:
    settings = get_settings()
    apply_paddlex_env(settings)
    setup_logging(
        settings.log_dir,
        level=str(settings.get("logging.level", "INFO")),
        max_bytes=int(settings.get("logging.max_bytes", 10 * 1024 * 1024)),
        backup_count=int(settings.get("logging.backup_count", 5)),
    )

    # 产线注册表（config/pipelines/*.yaml + 内置 OCR），支持任务级选择产线
    pipeline_specs = build_registry(settings)
    engines = EnginePool(settings, pipeline_specs)
    if engines.is_mock:
        get_logger("ocr.app").warning("已启用 MOCK 推理引擎（不加载模型，仅用于联调/自检）")
    get_logger("ocr.app").info(
        "可用产线：%s（默认 %s）",
        "、".join(f"{sp.id}" for sp in pipeline_specs),
        engines.default_id,
    )

    storage = Storage(settings)
    manager = TaskManager(settings, storage, engines)
    auth = AdminAuth(settings)
    # 前台匿名会话：不同浏览器/不同人之间任务隔离（签名 Cookie，无需登录）
    client_sessions = ClientSessions(
        settings.session_secret,
        cookie_name=str(settings.get("server.client_cookie_name", "pdx_sid") or "pdx_sid"),
        ttl_hours=float(settings.get("server.client_session_ttl_hours", 24) or 24),
    )

    app = FastAPI(
        title="PaddleX OCR Web",
        version=__version__,
        description="离线 OCR + 表格识别（PaddleX PP-StructureV3）",
        root_path=str(settings.get("server.root_path", "") or ""),
        # 关闭 FastAPI 自带的 /api/docs：它固定从 cdn.jsdelivr.net 拉
        # swagger-ui-bundle.js / swagger-ui.css，离线环境必然 404、页面全白。
        # 下面用本地资源重新注册 /api/docs。
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engines = engines
    app.state.engine = engines  # 兼容旧引用（健康检查/后台）
    app.state.pipeline_specs = pipeline_specs
    app.state.storage = storage
    app.state.manager = manager
    app.state.auth = auth
    app.state.client_sessions = client_sessions

    app.include_router(public_routes.router)
    app.include_router(admin_routes.router)

    # ------------------------------------------------------------- 中间件
    @app.middleware("http")
    async def client_session(request: Request, call_next):  # noqa: ANN001
        """为每个浏览器分配/校验匿名会话 id，并写入 request.state.sid。

        Cookie 值 = sid + HMAC 签名，篡改会被识别并重新签发；
        任务按 sid 归属，公开接口只返回本会话的数据。
        """
        sid, created = client_sessions.resolve(request.cookies.get(client_sessions.cookie_name))
        request.state.sid = sid
        response = await call_next(request)
        if created:
            response.set_cookie(
                key=client_sessions.cookie_name,
                value=client_sessions.cookie_value(sid),
                max_age=client_sessions.max_age,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # noqa: ANN001
        started = time.time()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            get_logger("ocr.access").exception("请求异常 %s %s", request.method, request.url.path)
            raise
        cost = (time.time() - started) * 1000
        path = request.url.path
        if not path.startswith(("/assets", "/api/health")) and cost > 5:
            get_logger("ocr.access").info(
                "%s %s -> %s (%.0fms)", request.method, path, response.status_code, cost
            )
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ANN001
        get_logger("ocr.app").exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误：{exc}"},
        )

    # ------------------------------------------------------------- 静态资源
    frontend = settings.frontend_dir
    assets_dir = frontend / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # ------------------------------------------------------------- API 文档（离线）
    # 用随包的 swagger-ui 资源渲染，不依赖任何 CDN：
    #   frontend/assets/swagger/swagger-ui-bundle.js  (约 1.6 MB)
    #   frontend/assets/swagger/swagger-ui.css        (约 180 KB)
    swagger_dir = assets_dir / "swagger"
    swagger_js = swagger_dir / "swagger-ui-bundle.js"
    swagger_css = swagger_dir / "swagger-ui.css"

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request):  # noqa: ANN201
        if not (swagger_js.is_file() and swagger_css.is_file()):
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "Swagger UI 静态资源缺失（离线部署不应访问 CDN）："
                    f"需要 {swagger_js} 与 {swagger_css}。"
                    "请确认离线包 frontend/assets/swagger/ 完整，或执行 "
                    "scripts/make_offline_bundle.sh 重新打包"
                },
            )
        # 反向代理子路径（server.root_path）下也要能取到资源
        prefix = str(request.scope.get("root_path", "") or "").rstrip("/")
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{app.title} · API 文档</title>
  <link rel="icon" href="{prefix}/assets/favicon.svg" />
  <link rel="stylesheet" href="{prefix}/assets/swagger/swagger-ui.css" />
</head>
<body>
<div id="swagger-ui"></div>
<script src="{prefix}/assets/swagger/swagger-ui-bundle.js"></script>
<script>
  window.ui = SwaggerUIBundle({{
    url: "{prefix}/api/openapi.json",
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    presets: [SwaggerUIBundle.presets.apis],
  }});
</script>
</body>
</html>"""
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    def _page(name: str) -> FileResponse:
        path = frontend / name
        if not path.exists():
            return JSONResponse(status_code=500, content={"detail": f"前端文件缺失：{path}"})  # type: ignore[return-value]
        return FileResponse(str(path), media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return _page("index.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return _page("admin.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        icon = frontend / "assets" / "favicon.svg"
        if icon.exists():
            return FileResponse(str(icon), media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=str(settings.get("server.host", "0.0.0.0")),
        port=int(settings.get("server.port", 8000)),
        workers=1,  # 必须单进程：任务队列在进程内
        log_level=str(settings.get("logging.level", "info")).lower(),
        access_log=False,
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
