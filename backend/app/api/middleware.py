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

from app.api.deps import assert_account_usable, load_account, session_factory
from app.core.config import settings
from app.core.enums import Role
from app.core.errors import Unauthenticated, error_body
from app.core.security import SessionInvalid, decode_session_token

AUTH_HEADER = "Authorization"
COOKIE_NAME = "wms_session"

#: 免认证白名单 —— **恰好这四条**，精确匹配。
#:
#: 措辞上的出入要说清，免得下一个人按注释去找事实来源却对不上：`13` §6.1 第 1 步字面写的是
#: 「检查请求路径是否匹配白名单**前缀**」，而同处列出的清单、以及 spec `permission`
#: 的「访问边界与免认证白名单」钉住的，都是**恰好四条路径**。本实现取**精确匹配** ——
#: 这是从 13 的清单收窄，不是照抄 13 的算法；宽的那一版会把 `/api/auth/login/extra`
#: 一并放行，而白名单每多一条就多一个免认证入口。
#:
#: 曾经还有一条 `/docs/` 前缀放行（怕 Swagger 页面加载子资源时被拦）。查证后删掉：
#: `/docs` 由 FastAPI 内联返回，JS/CSS 走 CDN，页面本身不请求任何本站子路径
#: （`/docs/oauth2-redirect` 只在 API 声明了 oauth2 安全方案时才会被取）。
#: 白名单每多一条就多一个免认证入口，而它服务的是「将来可能会用到」——
#: 真加了 oauth2 再连同这条注释一起恢复。
WHITELIST_EXACT = frozenset(
    {
        "/api/auth/login",
        "/health",
        "/docs",
        "/openapi.json",
    }
)


def _is_whitelisted(path: str) -> bool:
    return path in WHITELIST_EXACT


def _requires_auth(path: str) -> bool:
    """只有 /api/* 需要业务认证；前端静态页与白名单不拦截。"""
    if not path.startswith(settings.api_prefix):
        return False
    return not _is_whitelisted(path)


def _extract_token(request: Request) -> str:
    """凭据来源：`Authorization` 头优先，无头则看 Cookie。

    Cookie 那条**不是**额外发明的一条认证路径 —— 13 §6.1 逐字写「从请求头 / Cookie
    提取会话凭据」。本阶段登录端点只把凭据放在响应体里（不下发 Cookie），
    所以这条路径当前无人写入；它在这里是因为事实来源要求它对前端开放。
    """
    header = request.headers.get(AUTH_HEADER, "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    if header:
        return header.strip()
    return request.cookies.get(COOKIE_NAME, "")


def _unauthorized(message: str) -> JSONResponse:
    """401 的**统一**外形 —— 与登录端点那份走同一段渲染（`errors.error_body`）。

    中间件在异常处理器**之外**（它包着整个应用），所以这里不能抛 `HTTPException`
    —— 那会走 FastAPI 的处理器链，在中间件里抛出去只会变成 500。故这里自己构造
    `Unauthenticated` 再渲染它：状态码与响应体都取自异常类，而不是在这里另写一遍。
    """
    exc = Unauthenticated(message)
    return JSONResponse(status_code=exc.http_status, content=error_body(exc))


def _as_role(value: str) -> Role:
    """凭据里的 `role` 取值 → `Role` 成员；取值不在枚举内 → `SessionInvalid`（401）。

    **必须转成枚举，不能原样放字符串**：`permissions.check` / `check_config_scope`
    有 `isinstance(role, Role)` 守卫（`str` 枚举的 `hash` 取成员名，裸字符串查表会
    静默查不中并一律返回 False），原样传下去会让第 2 层在每个请求上抛 `TypeError`
    —— 把一次 401 变成一次 500，方向还恰好是最不该出错的那个方向。

    取值不合法也不是「权限不足」而是「这份凭据不该通行」：本系统只签发这四个角色
    （13 §2.2），出现第五个只能是被改过或跨版本。
    """
    try:
        return Role(value)
    except ValueError as exc:
        raise SessionInvalid("会话凭据的 role 取值不合法: %r" % (value,)) from exc


def _load_active_account(request: Request, account_id: int):
    """回查账号当前状态（13 §8.3），不可用则抛 `Unauthenticated`。

    会话是短命的：取完即关。**不复用端点的会话** —— 中间件在路由之前运行，
    若把自己的会话挂到 `request.state` 上，端点的 `get_db` 要么复用它（两个生命周期
    纠缠在一起，端点提交会带着中间件的读事务一起提交），要么再开一个（两倍的连接）。
    一行主键查询的成本远低于这两者。

    工厂取自 `deps.session_factory(request)` 而不是直接读 `request.app.state`：
    同一个属性的两种读法会漂移（`deps.py` 的模块 docstring 讲了为什么只有一个属性），
    而这种漂移的表现是「路由读测试库、中间件读开发库」。

    返回的行在会话关闭后是 detach 状态，列已全部加载，供端点读用（`deps.current_account`）。
    """
    with session_factory(request)() as session:
        return assert_account_usable(load_account(session, account_id))


class AuthMiddleware(BaseHTTPMiddleware):
    """全局认证。凭据缺失、无效、过期或签名错误一律 **401**。

    这里的硬边界是**不返回 404** —— 不泄露路由是否存在（spec `permission` 的
    「访问边界与免认证白名单」）。**不是**「不区分失败原因」：401 响应体经
    `_unauthorized` 带着 `SessionInvalid` 的逐因文案，这是**有意**的，理由与
    「它为何不构成探测面」写在 spec `auth` 的「登录端点与失败语义」里 ——
    逐因文案只在**签名校验通过之后**才可达（`security.py` 的校验顺序），
    与之相反的是登录端点：那里三种失败必须返回同一响应体。
    """

    async def dispatch(self, request: Request, call_next):
        if not _requires_auth(request.url.path):
            return await call_next(request)

        try:
            claims = decode_session_token(_extract_token(request))
            role = _as_role(claims["role"])
            account = _load_active_account(request, claims["user_id"])
        except SessionInvalid as exc:
            return _unauthorized(str(exc))
        except Unauthenticated as exc:
            return _unauthorized(str(exc))

        request.state.session = claims
        request.state.account = account
        request.state.account_id = account.id
        # 来自凭据（13 §6.2），不是来自上面那行 —— 见模块 docstring。
        # 是 `Role` 成员而非裸字符串，与 `permissions.check` 的类型守卫配套（见 _as_role）。
        request.state.role = role
        # spec `auth` 的「改密前不得访问其他业务端点」需要一个拦截点，本阶段**没有**实现它；
        # 标志先放这里，拦截与改密端点一起登记在 tasks.md 9.4g。
        request.state.must_change_password = not account.initial_password_changed

        return await call_next(request)
