"""服务可用性路由。

事实来源：19-系统架构与部署视图 附录B

`/health` 在认证白名单内（13 §6.1），用于探活。
`/api/health` 不在白名单内（理由见 app/api/middleware.py 的说明）。

本模块是骨架阶段唯一带真实实现的端点，用于验证应用能装配并启动。
"""
from fastapi import APIRouter

from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """探活（免认证）。"""
    return {
        "status": "ok",
        "app": settings.app_name,
        "warehouse": settings.warehouse_code,
    }


@router.get("/api/health")
def api_health() -> dict:
    """带认证的可用性检查（19 附录B）。"""
    return {
        "status": "ok",
        "cold_path_enabled": settings.cold_path_enabled,
    }
