"""身份链：Account → 角色 → 权限（1 个实体）。

事实来源：17-数据模型设计 §六（身份与权限实体）、§九（枚举）、§十一（数据隔离）
          13-权限分级与访问控制系统 §一（角色）、§5.1（状态流转图）、§5.2（账号数据模型）、
          §7.2（会话凭据的 claim 集合）、§8.3（无服务端 session）
          design.md D2（`AccountStatus` 补 `REJECTED`）、D3（取值约束落两层）、D6（bcrypt）、D8
          spec `auth`「账号由管理员开通」「账号状态迁移」「初始凭据与首次改密」

字段：用户名（全局唯一）、密码哈希、角色、状态、创建人、创建时间、
      最后登录时间、初始密码是否已修改。
账号状态机：pending → active / rejected；active → disabled → active
（迁移表与守卫属 §7，见 tasks.md 7.3；本模块只落实体）。

注：会话凭据是无状态的（13 §8.3），不是实体；AuditLog 为路线图（13 §4.4），暂不建。

## 本实体的四处取舍

1. **用户名全局唯一，不是「仓库内唯一」**。`17` §6.1 明写「全局唯一」，这是
   `warehouse_id` 过滤维度的一处刻意例外：账号是运维主体，同一人在两个厂的库里仍是
   同一个人。按仓库分域会让「同用户名两个账号」看起来合法，而登录页只有一个用户名
   输入框（22 §2.1）—— 撞名即无法定位账号。故唯一键落在 `username` 单列上，
   `warehouse_id` 仍按 §十一 保留（数据范围区分用，13 §3.2）。
2. **`created_by_id` 可空，且指向本表**。自举问题：13 §5.1 的状态图以「管理员创建账号」
   为起点，而第一个管理员没有上级可指 —— 用 NULL 表达「基建写入、无创建人」，
   不编造一个 `system` 账号来凑外键，那会在审计链上多出一个假节点。
3. **`last_login_at` 可空**，不填 `created_at` 兜底：空 = 从未登录。登录页的
   「初始密码未修改」提示（spec `auth`）与看板的账号状态都依赖这个「从未」，
   用一个看似合理的时间填上，就再也分不出「没登录过」与「很久没登录」。
4. **只有 `password_hash`，没有明文列**（spec `auth`：不得以明文或可逆方式存储）。
   形态是 bcrypt 的 60 字符串（D6），列宽留到 128 是给将来的算法迁移留位置 ——
   换算法时旧哈希要能原样读出来校验，不能因为列窄而回填不了。

`Role` 与 `AccountStatus` 保持 **lowercase**（D3）：这两个取值会写进 CHECK、进凭据的
`status` claim（13 §7.2），大小写是契约的一部分，不要顺手统一成大写。
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AccountStatus, Role
from app.models.base import BaseEntity, enum_column


class Account(BaseEntity):
    """账号（`17` §6.1 / `13` §5.2）。v1 本地基础账号；SSO 与精细 RBAC 为路线图。"""

    __tablename__ = "accounts"
    __table_args__ = (
        # 全局唯一（模块 docstring 第 1 条）—— 不含 warehouse_id。
        sa.UniqueConstraint("username", name="uq_accounts_username"),
    )

    #: 用户名，登录凭据的一半（22 §2.1 的登录表单）。长度 64：文档未给上界，
    #: 取一个足够宽又不会成为注入/存储负担的值（Open Question 里的 64 字符上限
    #: 指的是**密码**，不是用户名）。
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 密码哈希。**永不明文**（spec `auth`），校验走 `bcrypt`（D6：本机 passlib 与
    #: bcrypt 5.x 不兼容，直接用 bcrypt）。列宽见模块 docstring 第 4 条。
    password_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)

    #: 角色，限 13 §一 的四类（17 §九 的 `role`）。
    role: Mapped[Role] = enum_column(Role, name="role", nullable=False)

    #: 状态，限四值。默认 `pending` —— 13 §5.1：管理员创建账号后须**显式激活**，
    #: 默认即 `active` 会让「开通」与「激活」两个权限动作合并成一个。
    status: Mapped[AccountStatus] = enum_column(
        AccountStatus, name="account_status", nullable=False, default=AccountStatus.PENDING
    )

    #: 创建人（自引用，可空，见模块 docstring 第 2 条）。不建 `ondelete` ——
    #: 本系统「归档不删除 + 版本化」，不提供删除账号的路径。
    created_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("accounts.id"), nullable=True
    )

    #: 最后登录时间（模块 docstring 第 3 条：空 = 从未登录）。
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: 初始密码是否已修改。**它是强制改密的开关**（`13` §5.1 注明「首次登录强制改密」，
    #: spec `auth` 要求首次登录必须修改密码）。默认 `False` = 尚未修改 = 下次登录必须改。
    initial_password_changed: Mapped[bool] = mapped_column(nullable=False, default=False)
