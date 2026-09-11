"""第 1 层：认证中间件（全局认证，v1 实现）。

事实来源：13-权限分级与访问控制系统 §6.1

流程：
  1. 只守卫 `/api/*`；命中白名单则放行
  2. 从 Authorization 头 / Cookie 提取会话凭据并校验
  3. 凭据缺失或无效 → HTTP 401，前端引导至登录页

白名单（13 §6.1 逐字）：`/api/auth/login`、`/health`、`/docs`、`/openapi.json`

**一处文档不一致（已标记待确认，未擅自修正）**：19 号附录B 定义了 `GET /api/health`，
但 13 §6.1 的白名单只列了 `/health`。本实现按 13 号字面执行 —— `/health` 免认证、
`/api/health` 需认证。即**探活请用 `/health`**。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.security import SessionInvalid, decode_session_token

AUTH_HEADER = "Authorization"
COOKIE_NAME = "wms_session"

#: 13 §6.1 白名单 —— 精确匹配。
WHITELIST_EXACT = frozenset(
    {
        "/api/auth/login",
        "/health",
        "/docs",
        "/openapi.json",
    }
)

#: /docs 页面会继续加载子资源，需按前缀放行。
WHITELIST_PREFIXES = ("/docs/",)


def _is_whitelisted(path: str) -> bool:
    return path in WHITELIST_EXACT or path.startswith(WHITELIST_PREFIXES)


def _requires_auth(path: str) -> bool:
    """只有 /api/* 需要业务认证；前端静态页与白名单不拦截。"""
    if not path.startswith(settings.api_prefix):
        return False
    return not _is_whitelisted(path)


def _extract_token(request: Request) -> str:
    header = request.headers.get(AUTH_HEADER, "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    if header:
        return header.strip()
    return request.cookies.get(COOKIE_NAME, "")


class AuthMiddleware(BaseHTTPMiddleware):
    """全局认证。凭据有问题一律 401，不区分原因（避免成为探测面）。"""

    async def dispatch(self, request: Request, call_next):
        if not _requires_auth(request.url.path):
            return await call_next(request)

        try:
            claims = decode_session_token(_extract_token(request))
        except SessionInvalid as exc:
            return JSONResponse(
                status_code=401,
                content={"error": "unauthenticated", "message": str(exc)},
            )

        # TODO(阶段二 / 文档 26)：回查 Account 当前 status 并断言为 active。
        #   13 §8.3 的紧急吊销依赖这一步 —— 管理员置 disabled 后下次请求即失败。
        #   只校验 token 内的 status 快照是不够的：旧 token 在 8 小时有效期内
        #   仍会显示 active，吊销不会立即生效。
        #   该回查需要 Account 模型，随数据模型（阶段二）一起落地。
        request.state.session = claims
        request.state.account_id = claims["user_id"]
        request.state.role = claims["role"]

        return await call_next(request)
