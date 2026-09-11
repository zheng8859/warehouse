"""FastAPI 依赖注入。

事实来源：13-权限分级与访问控制系统 §六（三层检查）、§8.3（紧急吊销：回查当前状态）

  `get_db`           → 数据库会话
  `current_account`  → 当前账号（第 1 层认证的产物）
  `require(resource, action)` → 第 2 层资源级权限检查（路线图 RBAC 的挂载点，见 D9）

## 会话工厂读的是 `app.state.session_factory`

中间件（第 1 层）与端点（依赖注入）**必须看到同一个库**。若各自直接 `import`
模块级的 `SessionLocal`，那么测试里想换成测试库就得同时打两个补丁 ——
漏掉一个的后果是「端点读测试库、中间件读开发库」，而它几乎必然表现为一个
难以理解的 401 或 500，而不是「有东西没配好」。

故 `create_app()` 把工厂挂到 `app.state.session_factory`，两处都从这里取。
**一次覆盖，两处生效**（`tests/api/test_auth.py` 的夹具自检盯着这件事）。
"""
from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import AccountStatus
from app.core.errors import Unauthenticated
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


def load_account(session: Session, account_id: object) -> Account | None:
    """按主键取账号。`account_id` 来自凭据载荷，故可能是任何 JSON 值。

    非整数 id 不会抛错：SQLite 按等值比较，取不到就是 `None` ——
    于是「伪造一份 `user_id` 类型奇怪的凭据」与「凭据指向已不存在的账号」
    走同一条路（都是 401），不会分叉出一个 500。
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
