"""认证 路由。

事实来源：19-系统架构与部署视图 附录B（`POST /api/auth/login`）
          13-权限分级与访问控制系统 §6.1（白名单）、§7.1（密码策略）、§7.2（会话态）、
          §7.3（前端凭据管理）、§8.1（客户端存储）、§8.3（无服务端 session）
          22-前端规格与设计系统 §2.1（登录页）
          spec `auth`「登录端点与失败语义」「无状态会话凭据」「初始密码与首次强制改密」

## 失败语义：**一种**失败

用户名不存在、口令错误、账号未激活 / 已停用 / 已驳回 —— 五种形态返回**逐字相同**的
401。原因不是省事：只要响应里有任何一处能区分它们，登录页就成了一台账号存在性探测器，
而枚举出有效用户名是口令爆破的第一步。22 §2.1 的失败提示也只有一条文案
（「账号或密码错误」），所以这里没有可牺牲的表达力。

两处容易被漏掉的：

  - **判定顺序：先口令、后状态。** 反过来（先看状态）会让「状态不是 active」的账号
    在 bcrypt 之前返回 —— 于是**不需要**正确口令，仅凭响应快慢就能问出
    「这个用户名存在，而且被停用了」。
  - **用户名不存在时照样做一次口令校验**（对着一份常驻的填充哈希）。否则
    「不存在」会立刻返回，而 bcrypt 一次几十毫秒：响应时间本身还是探测器
    （快 = 不存在）。响应体一致只挡住了**读**，挡不住**计时**。

## 三处与「认证」相邻但不属于它的东西

  - **凭据只在响应体里给，不下发 Cookie**。13 §7.3 / §8.1：凭据由客户端持久化存储、
    请求时拼进认证头。两处都写会让「登出要清哪个」变成真问题；服务端无状态（§8.3），
    登出就是前端清掉自己那份，故本阶段**不实现登出端点**（不需要它存在）。
  - **`must_change_password` 只给标记，不做拦截**。spec 的「改密前不得访问其他业务端点」
    与本文件同属一条 Requirement，但 tasks §7 没有为它派任务 —— 拦截与改密端点
    一起登记在 tasks.md 9.4g，不在这里顺手实现半个。
  - **`last_login_at` 只在成功时写**（13 §5.2 的账号字段）。失败的那次不留痕：
    这个字段是「最后登录时间」，不是「最后尝试时间」，两者混用会让审计读错。
"""
from __future__ import annotations

import secrets
from functools import lru_cache

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.enums import AccountStatus
from app.core.errors import Unauthenticated
from app.core.security import create_session_token, hash_password, verify_password
from app.models.base import utcnow
from app.models.identity import Account
from app.schemas.auth import LoginRequest, LoginResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: 登录失败的**唯一**说法。文案取自 22 §2.1 的登录页红字。
LOGIN_FAILED = "账号或密码错误"


@lru_cache(maxsize=1)
def _padding_hash() -> str:
    """一份常驻的假哈希，只用来在「用户名不存在」时消耗掉一次 bcrypt。

    惰性生成（首次用到才算）：进程启动时算一次哈希，会平白拖慢每一个启动路径 ——
    包括那些根本没人登录的（迁移、建库、测试收集）。成本因子与真实哈希同源
    （`settings.bcrypt_cost`），否则填充校验与真实校验耗时不同，计时差又回来了。
    """
    return hash_password(secrets.token_urlsafe(32))


def _authenticate(db: Session, username: str, password: str) -> Account | None:
    """校验用户名与口令，成功且状态可用于登录时返回账号行。

    判定顺序见模块 docstring：**先口令、后状态**。三条失败路径都恰好消耗一次
    bcrypt（`verify_password` 对畸形哈希与超长输入提前返回 `False`，那两种情况
    本身就是外部无法构造的库内状态）。
    """
    account = db.execute(
        select(Account).where(Account.username == username)
    ).scalar_one_or_none()

    if account is None:
        verify_password(password, _padding_hash())  # 抹平时序差，结果丢弃
        return None

    if not verify_password(password, account.password_hash):
        return None

    if account.status is not AccountStatus.ACTIVE:
        return None

    return account


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """登录（免认证，13 §6.1 白名单）。成功返回凭据与前端要的账号事实。"""
    account = _authenticate(db, payload.username, payload.password)
    if account is None:
        # 走 `DomainError` 处理器 → 401 `{"error": "unauthenticated", "message": …}`，
        # 与中间件那份 401 **同形**：前端的拦截器按 `error` 分流（401 → 跳登录页），
        # 两个产生 401 的地方形状不同，它就得写两个分支。
        raise Unauthenticated(LOGIN_FAILED)

    # 只有走到这里才是「登录过」（模块 docstring 末条）。
    account.last_login_at = utcnow()
    db.commit()

    token = create_session_token(
        user_id=account.id,
        role=account.role.value,
        status=account.status.value,
    )
    return LoginResponse(
        access_token=token,
        expires_in=settings.session_hours * 3600,
        user_id=account.id,
        role=account.role,
        warehouse_id=settings.warehouse_code,
        must_change_password=not account.initial_password_changed,
    )
