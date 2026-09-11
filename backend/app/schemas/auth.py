"""认证报文的请求 / 响应 DTO。

事实来源：13-权限分级与访问控制系统 §7.2（会话态字段）
          spec `auth`「登录端点与失败语义」（免认证、一律 401、不区分失败原因）、
          「无状态会话凭据」（claim 集合、8 小时）、「初始密码与首次强制改密」
          22-前端规格与设计系统 §2.1（登录页：用户名 + 密码两栏；成功跳转）

**这不是 `17` §十 的那 6 类 JSON 结构之一**。那 6 类是领域载荷（推荐理由、cap 快照…），
存进实体列里；本文件是**接口层**的报文，只活在 HTTP 边界上，没有对应的实体列。

三处开发阶段决策（13 / 22 只规定语义，未规定报文格式）：

1. **JSON 而不是表单**。前端是零构建 Vanilla JS 的同源 `fetch`，JSON 是它的原生形态；
   表单需要在客户端 urlencode、在服务端引入 form 解析依赖，而这条路径上没有文件要传。
2. **请求体字段名就是 `username` / `password`**，不套 `OAuth2PasswordRequestForm`。
   那套形态带着 `grant_type` / `scope` 等本产品不使用的字段，且它的字段名约定
   （`username` 作为"用户名"位）只对 OAuth2 有意义 —— 引进来只会让人以为这里有 OAuth。
3. **不对 `password` 设长度上限**。上限 72 字节是 bcrypt 的**实现事实**（见
   `app/core/security.py`），不是产品策略；把它写成报文校验会让超长口令得到 422
   而其它失败得到 401 —— 同一件事出现两种失败形状，正是 spec 要避免的。
   超长口令照常走校验、照常返回那份唯一的 401。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Role

#: 用户名列宽（`Account.username` 是 `String(64)`）。报文层先挡一道：
#: 超长用户名进入查询只是白跑一次库，而它是外部可控的输入。
USERNAME_MAX_LENGTH = 64


class LoginRequest(BaseModel):
    """`POST /api/auth/login` 的请求体。"""

    #: 未知字段直接 422：让「把 username 拼成 user_name」在前端联调时就暴露，
    #: 而不是生产上表现为一句「账号或密码错误」。
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    """登录成功的响应体。

    凭据按 13 §7.3 / §8.1 由**客户端**持久化存储，请求时拼进认证头 ——
    故这里只在响应体里给出，不下发 Cookie：两处都写会让「登出要清哪个」变成
    一个真实的问题（Cookie 清不掉 HttpOnly 之外的东西，而前端还要清存储）。
    服务端无状态（13 §8.3），登出就是前端清掉它自己那份。
    """

    access_token: str
    token_type: str = "bearer"
    #: 有效秒数。取自 `exp - iat`，与凭据里的 `exp` 同源 —— 不是另一份配置读数。
    expires_in: int
    #: 13 §8.1 的「会话级存储（JSON：user_id/role）」：前端据此定菜单可见性，
    #: 不必自己解开载荷（那等于把签名验证的逻辑抄进前端）。
    user_id: int
    role: Role
    warehouse_id: str
    #: 首次登录须改密（13 §7.1 密码策略）。**只给标记，不做拦截** ——
    #: 拦截属路线图，见 tasks.md 9.4g 的登记。
    must_change_password: bool
