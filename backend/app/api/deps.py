"""FastAPI 依赖注入。

事实来源：13-权限分级与访问控制系统 §六（三层检查）、§8.3（紧急吊销：回查当前状态）

  `session_factory`  → 当前应用的会话工厂（中间件也用同一个）
  `get_db`           → 数据库会话
  `current_account`  → 当前账号（第 1 层认证的产物）

**第 2 层在这里起步**：矩阵本身在 `app/api/permissions.py`（13 §2.2 的逐条搬运），
`require_permission(permission)` 把它挂成依赖注入。v1 只对 `POST /api/allocate/batch`
施加 `inbound.operate`；其余业务端点的资源级鉴权仍是路线图 RBAC（M5）的工作，故此处
**不预置**一个永远返回「允许」的空壳 `require()` —— 一个永远放行的检查器比没有它更糟，
读的人会以为某条路径已经被强制了。

## 会话工厂读的是 `app.state.session_factory`

中间件（第 1 层）与端点（依赖注入）**必须看到同一个库**。若各自直接 `import`
模块级的 `SessionLocal`，那么测试里想换成测试库就得同时打两个补丁 ——
漏掉一个的后果是「端点读测试库、中间件读开发库」，而它几乎必然表现为一个
难以理解的 401 或 500，而不是「有东西没配好」。

故 `create_app()` 把工厂挂到 `app.state.session_factory`，两处都从这里取。
**一次覆盖，两处生效**（`tests/api/test_auth.py` 的夹具自检盯着这件事）。
"""
from __future__ import annotations

from collections.abc import Callable, Generator

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from app.api.permissions import Permission, check
from app.core.config import settings
from app.core.enums import AccountStatus
from app.core.errors import ColdPathDisabled, PermissionDenied, Unauthenticated
from app.models.identity import Account

#: 账号不可用时的统一说法。**不区分**「不存在」与「状态不是 active」：
#: 状态本身是敏感信息（离职 / 调岗 / 驳回），而持凭据的人能做的事都是找管理员。
ACCOUNT_UNAVAILABLE = "账号不可用，请联系管理员"


def session_factory(request: Request) -> sessionmaker[Session]:
    """当前应用的会话工厂（见模块 docstring）。"""
    return request.app.state.session_factory


def get_db(request: Request) -> Generator[Session, None, None]:
    """每请求一个会话，异常时回滚，结束时关闭。

    **不在这里提交**：事务边界属于端点（`db.py` 的同一约定：cap 与台账同事务写入
    必须由调用方划定边界）。登录写 `last_login_at` 那一处自己 `commit`。
    """
    session = session_factory(request)()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def load_account(session: Session, account_id: int) -> Account | None:
    """按主键取账号。行不在时返回 `None`。

    于是「凭据指向已不存在的账号」（库被重建、账号被迁移、有人手工改过库）与
    「账号状态不是 active」走同一条路：401，而不是 500。

    `account_id` 的类型**由 `decode_session_token` 保证**（`_CLAIM_TYPES` 要求非布尔的
    `int`），所以这里不再自己校验一遍。不校验的理由不是省事：`session.get()` 收到
    `dict` 会抛 `InvalidRequestError`（不是返回 `None`），写成「反正取不到就是 None」
    会让这条注释在真出事时是错的。校验只在一处、且在上游 —— 签名标 `int` 把这件事
    说清楚，越界即调用方的错。
    """
    return session.get(Account, account_id)


def assert_account_usable(account: Account | None) -> Account:
    """账号必须存在且 `status == active`，否则 401（13 §8.3 的紧急吊销）。

    判定写成**正向**的 `is not ACTIVE` 而不是「拦住 disabled」：后者会放过
    `pending` / `rejected`，而这三个非 active 状态在门禁上的语义完全一样 ——
    「这份凭据不该通行」。
    """
    if account is None or account.status is not AccountStatus.ACTIVE:
        raise Unauthenticated(ACCOUNT_UNAVAILABLE)
    return account


def current_account(request: Request) -> Account:
    """当前账号 —— 中间件已经加载并校验过，这里只取回。

    刻意**不**再查一次库：中间件与本依赖若各查一次，两次之间账号可能被停用，
    于是同一个请求里「门禁判定」与「业务读取」看到两个不同的状态。
    中间件把行放进 `request.state.account`，全请求只此一份。
    """
    account = getattr(request.state, "account", None)
    if account is None:
        # 走到这里说明端点被挂在了 `/api/*` 之外（中间件不管辖），
        # 或 dependency_overrides 绕开了中间件。两种都是装配错误，且不安全。
        raise Unauthenticated("缺少会话凭据")
    return account


def require_permission(permission: Permission) -> Callable[[Request], None]:
    """第 2 层资源级鉴权依赖：当前角色须持有 `permission`，否则 403。

    角色取自 `request.state.role`（中间件已把凭据里的 `role` 转成 `Role` 成员，
    见 `middleware._as_role`）。**不复用 `current_account`**：它返回 `Account`，
    而角色在凭据里（13 §6.2「从凭据解析 user_id、role」），不在账号行上。

    取不到 role（端点被挂在 `/api/*` 之外、中间件不管辖）按未认证处理 —— 与
    `current_account` 的同一处置：那是装配错误，不是「权限不足」。

    v1 只有 `POST /api/allocate/batch` 用它（`inbound.operate`）；其余端点的资源级
    鉴权是路线图 RBAC（M5）的工作，故本依赖不设一个「允许一切」的默认行为。
    """
    def _checker(request: Request) -> None:
        role = getattr(request.state, "role", None)
        if role is None:
            raise Unauthenticated("缺少会话凭据")
        if not check(role, permission):
            raise PermissionDenied(
                f"角色 {role.value} 无权执行 {permission.value}",
                detail={"role": role.value, "permission": permission.value},
            )

    return _checker


def require_cold_path_enabled() -> None:
    """冷路径开关守卫（D2）：关闭（默认）→ 409 `cold_path_disabled`。`toggle` 不挂此依赖。

    开关关闭 = 「能力整体关闭」是可纠正客户端错误（先开再调）→ 409；LLM 侧失败 =
    「能力开了但没吐字」是降级 → 200 + `degraded_reason`。两者不能都 200（D2），否则
    前端无法区分「去开开关」与「AI 临时不可用」。

    供两处复用（`routes/llm.py` 的五个能力端点、`routes/conversation.py` 的统一入口），
    文案**只此一份** —— 前端据此分流「功能没开」与「AI 没吐字」。
    """
    if not settings.cold_path_enabled:
        raise ColdPathDisabled(
            "冷路径未开启（cold_path_enabled=false）—— 请管理员先经 POST /api/llm/toggle 开启"
        )
