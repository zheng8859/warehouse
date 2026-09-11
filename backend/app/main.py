"""FastAPI 应用装配。

事实来源：19-系统架构与部署视图 §2.2（五层架构）、§3.3（单应用 + 单库）、附录B（路由）

启动方式（单进程，不得加 --workers，见 core/db.py 的并发约束）：

    uvicorn app.main:app --host 0.0.0.0 --port 8000

阶段一（文档 25）只保证装配骨架可启动；各路由的具体实现在阶段二~七落地。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.middleware import AuthMiddleware
from app.api.routes import (
    allocate,
    auth,
    cap,
    health,
    import_,
    job,
    kpi,
    llm,
    snapshot,
)
from app.core.config import settings
from app.core.errors import DomainError

logger = logging.getLogger(__name__)

#: 路由模块清单。顺序不影响匹配（路径不重叠），但保持与 19 附录B 一致便于对照。
ROUTE_MODULES = (
    auth,
    import_,
    snapshot,
    cap,
    allocate,
    job,
    kpi,
    llm,
    health,
)


def create_app() -> FastAPI:
    settings.assert_production_safe()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        # 这两个路径必须在认证白名单内（13 §6.1），不要改动。
        docs_url="/docs",
        openapi_url="/openapi.json",
        redoc_url=None,
    )

    # 第 1 层：认证中间件（全局认证，v1 实现）。13 §六。
    app.add_middleware(AuthMiddleware)

    register_exception_handlers(app)

    for module in ROUTE_MODULES:
        app.include_router(module.router)

    @app.on_event("startup")
    async def _log_startup() -> None:
        logger.info(
            "%s 启动 | 环境=%s | 工厂=%s | 库=%s | 冷路径=%s",
            settings.app_name,
            settings.environment,
            settings.warehouse_code,
            settings.database_url,
            "开启" if settings.cold_path_enabled else "关闭",
        )

    # 阶段七（文档 31）在此挂载前端静态页：
    #   app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
    # 前端为零构建 Vanilla HTML/CSS/JS，不引入打包步骤。

    return app


def register_exception_handlers(app: FastAPI) -> None:
    """把领域异常映射为 HTTP 状态码。

    约定（13 §六 / 16 §二）：
      401 凭据问题（由中间件直接返回）
      403 权限不足
      409 状态机冲突 / 乐观锁版本不一致
      422 校验失败（校验失败阻断，不得带病入库）
    """

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )


app = create_app()
