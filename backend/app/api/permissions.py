"""第 2 层：PermissionChecker（资源级权限，路线图）。

事实来源：13-权限分级与访问控制系统 §2.1 / §2.2 / §2.3 / §3.1
          22-前端页面设计规格 §2.1 ⑦（配置页的 5 个子模块）
          spec `auth`（角色与权限）、tasks.md §8（补测范围）

**v1 事实：细粒度 RBAC 未实现。** 首期只有角色菜单可见性 + 写操作二次确认。
本模块的 `ROLE_PERMISSIONS` 是**权威目标模型**，RBAC 落地后（M5）由此强制。
无权时返回 HTTP 403。

本文件是 13 号 §2.2 矩阵的逐条搬运。改动矩阵前先改 13 号文档。

红线条目：`ledger.write` 与 `engine.invoke` 在矩阵中标为「自动」= 由作业流 / 引擎
执行，**对所有业务角色都不开放手动入口**。`check()` 对这两个权限恒返回 False，
不得为了「方便调试」给任何角色打开。

## 两个维度，两个函数

`check(role, permission)` 答「这个角色有没有这个权限」；`check_config_scope(role, scope)`
答「在配置页里，这个子模块归不归它管」。13 §2.2 用 🔧 表示后者受限，那是**两问合一**的
写法 —— 折成一个布尔就必须把 `scope` 塞进 `check`，而 `check` 的其余 16 格没有这个概念。
拆开后，主管的 `config.configure` 是 `True`（有权进入配置页）且 `check_config_scope`
只对 `weight` 放行，两者合起来才是 🔧。

## 类型守卫：让说谎的调用**报错**，而不是静默拒绝

`Role` 与 `Permission` 都是 `str` 枚举，于是 `check("admin", "kpi.view")` 看起来完全
合理（`AccountStatus.ACTIVE == "active"` 这类值比较都能过），但字典与集合查的是
`hash`，而 `Enum.__hash__` 取**成员名**：`ROLE_PERMISSIONS.get("admin")` 落在
成员名 `ADMIN` 上，查不中 → 返回 `False`。fail-closed 的方向没错，但现场表现是
「管理员登录后菜单全空」，而日志里一句错都没有 —— 这类静默比崩溃贵得多。

所以两个函数都先 `isinstance` 校验入参，裸字符串一律 `TypeError`（与
`app/core/state_machine.py`、`app/core/account_state.py` 同一处置）。
"""
from __future__ import annotations

from enum import Enum

from app.core.enums import Role


class Permission(str, Enum):
    """资源.操作 标识。13 §2.1 —— 共 17 个。"""

    ACCOUNT_MANAGE = "account.manage"

    DATA_IMPORT_VIEW = "data_import.view"
    DATA_IMPORT_OPERATE = "data_import.operate"

    INBOUND_VIEW = "inbound.view"
    INBOUND_OPERATE = "inbound.operate"

    OUTBOUND_VIEW = "outbound.view"
    OUTBOUND_OPERATE = "outbound.operate"

    RELOCATE_VIEW = "relocate.view"
    RELOCATE_OPERATE = "relocate.operate"

    KPI_VIEW = "kpi.view"

    CONFIG_VIEW = "config.view"
    CONFIG_CONFIGURE = "config.configure"

    CONVERSATION_VIEW = "conversation.view"
    CONVERSATION_OPERATE = "conversation.operate"

    LEDGER_VIEW = "ledger.view"
    LEDGER_WRITE = "ledger.write"

    ENGINE_INVOKE = "engine.invoke"


#: 恒不授予任何角色的权限 —— 只能由系统内部（作业流 / 引擎）触发。
AUTO_ONLY: frozenset[Permission] = frozenset(
    {
        Permission.LEDGER_WRITE,
        Permission.ENGINE_INVOKE,
    }
)

#: 配置页的 5 个子模块标识（13 §2.1 的 config 行 / 22 §2.1 ⑦），**顺序即文档的 ①~⑤**。
#:
#: 13 与 22 都只给了中文名（导入模板 / cap 口径 / 评分因子权重 / 近站台预留比例 /
#: 对话指令词），标识符本身留给了实现。这五个 ASCII 名是**契约**而非内部细节：
#: 阶段七的配置页按它渲染子导航胶囊、按它决定隐藏哪些，任何一侧改名都要求另一侧
#: 同步。已登记 tasks.md 9.4h —— 改名前先确认前端没有被落下。
#:
#: `weight` 沿用既有取值：`SUPERVISOR_CONFIG_SCOPES` 早就写着它，改它是一次纯粹的
#: 无收益返工，而这两个集合必须用同一套字面量（下方有一处断言守着包含关系）。
CONFIG_SCOPES: tuple[str, ...] = (
    "import_template",   # ① 导入模板
    "cap_caliber",       # ② cap 口径
    "weight",            # ③ 评分因子权重
    "reserve_ratio",     # ④ 近站台预留比例
    "prompt",            # ⑤ 对话指令词
)

#: 主管在 config 上受限：仅权重子模块可看可改（13 §2.2 的 🔧、§3.1 的「🔧 权重」）。
#: 其余配置子模块（导入模板 / cap 口径 / 近站台预留比例 / 对话指令词）不可见。
#:
#: 22 §2.1 ⑦ 说配置页「管理员专用」，与这里的 🔧 不冲突：前者说的是**页面的默认归属**，
#: 后者说的是主管在页内被限制到的一个子模块。13 §3.1 的菜单表逐字写着
#: `配置页 | — | — | 🔧 权重 | ✅`，13 §5.x 又写「配置页中本角色有权限的子模块
#: （如主管仅权重）」/「跨子模块越权配置」。矩阵以 13 为准（CLAUDE.md §八）。
SUPERVISOR_CONFIG_SCOPES: frozenset[str] = frozenset({"weight"})


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.WAREHOUSE_KEEPER: frozenset(
        {
            Permission.DATA_IMPORT_VIEW,
            Permission.DATA_IMPORT_OPERATE,
            Permission.INBOUND_VIEW,
            Permission.INBOUND_OPERATE,
            Permission.OUTBOUND_VIEW,
            Permission.OUTBOUND_OPERATE,
            Permission.RELOCATE_VIEW,
            Permission.RELOCATE_OPERATE,
            Permission.KPI_VIEW,
            Permission.CONVERSATION_VIEW,
            Permission.CONVERSATION_OPERATE,
            Permission.LEDGER_VIEW,
        }
    ),
    Role.PLANNER: frozenset(
        {
            Permission.DATA_IMPORT_VIEW,
            Permission.KPI_VIEW,
            Permission.CONVERSATION_VIEW,
            Permission.CONVERSATION_OPERATE,
            Permission.LEDGER_VIEW,
        }
    ),
    Role.SUPERVISOR: frozenset(
        {
            # 注意：主管没有 inbound.view —— 入库执行不归主管。
            Permission.OUTBOUND_VIEW,
            Permission.RELOCATE_VIEW,
            Permission.RELOCATE_OPERATE,
            Permission.KPI_VIEW,
            Permission.CONFIG_VIEW,  # 仅权重子模块，见 SUPERVISOR_CONFIG_SCOPES
            Permission.CONFIG_CONFIGURE,  # 同上
            Permission.CONVERSATION_VIEW,
            Permission.CONVERSATION_OPERATE,
            Permission.LEDGER_VIEW,
        }
    ),
    Role.ADMIN: frozenset({p for p in Permission if p not in AUTO_ONLY}),
}


def _require_role(role: object) -> Role:
    """角色入参必须是 `Role` 成员。理由见模块 docstring「类型守卫」。"""
    if not isinstance(role, Role):
        raise TypeError(
            f"role 必须是 Role 枚举成员，收到 {type(role).__name__}: {role!r} —— "
            "裸字符串会因 Enum.__hash__ 取成员名而静默查不中，一律返回 False"
        )
    return role


def _require_permission(permission: object) -> Permission:
    """权限入参必须是 `Permission` 成员。理由同 `_require_role`。"""
    if not isinstance(permission, Permission):
        raise TypeError(
            f"permission 必须是 Permission 枚举成员，收到 "
            f"{type(permission).__name__}: {permission!r}"
        )
    return permission


def check(role: Role, permission: Permission) -> bool:
    """判断角色是否拥有某权限。

    AUTO_ONLY 权限恒为 False —— 台账写入与引擎调用不由角色触发。这一条**先于**
    查表：即便将来有人把这两个权限误加进某个角色的集合，这里也不放行（测试
    `test_auto_only_permissions_are_never_granted` 守着它）。
    """
    role = _require_role(role)
    permission = _require_permission(permission)

    if permission in AUTO_ONLY:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def check_config_scope(role: Role, scope: str) -> bool:
    """配置页的子模块级检查（v1 只有主管需要区分）。

    主管仅在 `SUPERVISOR_CONFIG_SCOPES` 内通过；其余角色不通过。

    管理员是**无条件的 True**，不查 `CONFIG_SCOPES`：13 §2.3 的「跨角色兜底」若建立
    在范围表上，那么将来新增第六个子模块时，管理员会在新子模块上线的那一刻**失去
    它** —— 而那张表未必同时更新。兜底的语义是「不受范围约束」，不是「范围的全集」。
    """
    role = _require_role(role)

    if role is Role.ADMIN:
        return True
    if role is Role.SUPERVISOR:
        return scope in SUPERVISOR_CONFIG_SCOPES
    return False
