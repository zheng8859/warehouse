"""第 1 层：认证中间件（全局认证，v1 实现）。

事实来源：13-权限分级与访问控制系统 §6.1（三层检查的第 1 层）、§8.3（紧急吊销）
          spec `auth`「无状态会话凭据」「紧急吊销」

流程：
  1. 只守卫 `/api/*`；命中白名单则放行
  2. 从 Authorization 头 / Cookie 提取会话凭据并校验
  3. **回查账号当前状态**，必须是 `active`
  4. 凭据缺失、无效或账号不可用 → HTTP 401，前端引导至登录页

白名单（13 §6.1 逐字）：`/api/auth/login`、`/health`、`/docs`、`/openapi.json`

## 第 3 步是这一步的全部意义所在

凭据是**无状态**的（13 §8.3：无服务端 session、无黑名单），载荷里那份 `status`
只是**签发时**的快照。只信它的话，管理员把账号置为 `disabled` 之后，旧凭据在余下的
8 小时里照样通行 —— 而「置 disabled 即失效」正是本产品唯一的紧急吊销手段。

所以这里查一次库。代价是每个 `/api/*` 请求多一次主键查询（单进程 SQLite，
主键命中），换来的是吊销即时生效、且不需要重启服务。

**`role` 取凭据而非库**：13 §6.2 逐字写「从凭据解析用户信息（user_id、role）」，
而 §8.3 只对 `status` 给了回查依据。两者的差别是真实存在的（管理员改了某人的角色，
旧凭据在有效期内仍按旧角色判定），登记在 tasks.md 9.4g，不在实现里单方面决定。

**一处文档不一致（已标记待确认，未擅自修正）**：19 号附录B 定义了 `GET /api/health`，
但 13 §6.1 的白名单只列了 `/health`。本实现按 13 号字面执行 —— `/health` 免认证、
`/api/health` 需认证。即**探活请用 `/health`**。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.deps import assert_account_usable, load_account
from app.core.config import settings
from app.core.errors import Unauthenticated
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


def _unauthorized(message: str) -> JSONResponse:
    """401 的**统一**外形。

    中间件在异常处理器**之外**（它包着整个应用），所以这里不能抛 `HTTPException`
    —— 那会走 FastAPI 的处理器链，在中间件里抛出去只会变成 500。响应形状就地从
    这份 dict 来，与登录端点的 401（`DomainError` → 处理器）保持同形。
    """
    return JSONResponse(
        status_code=401,
        content={"error": "unauthenticated", "message": message},
    )


def _load_active_account(request: Request, account_id: object):
    """回查账号当前状态（13 §8.3），不可用则抛 `Unauthenticated`。

    会话是短命的：取完即关。**不复用端点的会话** —— 中间件在路由之前运行，
    若把自己的会话挂到 `request.state` 上，端点的 `get_db` 要么复用它（两个生命周期
    纠缠在一起，端点提交会带着中间件的读事务一起提交），要么再开一个（两倍的连接）。
    一行主键查询的成本远低于这两者。

    返回的行在会话关闭后是 detach 状态，列已全部加载，供端点读用（`deps.current_account`）。
    """
    factory = request.app.state.session_factory
    with factory() as session:
        return assert_account_usable(load_account(session, account_id))


class AuthMiddleware(BaseHTTPMiddleware):
    """全局认证。凭据有问题一律 401，不区分原因（避免成为探测面）。"""

    async def dispatch(self, request: Request, call_next):
        if not _requires_auth(request.url.path):
            return await call_next(request)

        try:
            claims = decode_session_token(_extract_token(request))
            account = _load_active_account(request, claims["user_id"])
        except SessionInvalid as exc:
            return _unauthorized(str(exc))
        except Unauthenticated as exc:
            return _unauthorized(str(exc))

        request.state.session = claims
        request.state.account = account
        request.state.account_id = account.id
        # 来自凭据（13 §6.2），不是来自上面那行 —— 见模块 docstring。
        request.state.role = claims["role"]
        # spec `auth` 的「改密前不得访问其他业务端点」需要一个拦截点，本阶段**没有**实现它；
        # 标志先放这里，拦截与改密端点一起登记在 tasks.md 9.4g。
        request.state.must_change_password = not account.initial_password_changed

        return await call_next(request)
