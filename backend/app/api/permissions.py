"""第 2 层：PermissionChecker（资源级权限，路线图）。

事实来源：13-权限分级与访问控制系统 §2.1 / §2.2

**v1 事实：细粒度 RBAC 未实现。** 首期只有角色菜单可见性 + 写操作二次确认。
本模块的 `ROLE_PERMISSIONS` 是**权威目标模型**，RBAC 落地后（M5）由此强制。
无权时返回 HTTP 403。

本文件是 13 号 §2.2 矩阵的逐条搬运。改动矩阵前先改 13 号文档。

红线条目：`ledger.write` 与 `engine.invoke` 在矩阵中标为「自动」= 由作业流 / 引擎
执行，**对所有业务角色都不开放手动入口**。`check()` 对这两个权限恒返回 False，
不得为了「方便调试」给任何角色打开。
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

#: 主管在 config 上受限：仅权重子模块可看可改（13 §2.2 的 🔧）。
#: 其余配置子模块（导入模板 / cap 口径 / 近站台预留比例 / 对话指令词）不可见。
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


def check(role: Role, permission: Permission) -> bool:
    """判断角色是否拥有某权限。

    AUTO_ONLY 权限恒为 False —— 台账写入与引擎调用不由角色触发。
    """
    if permission in AUTO_ONLY:
        return False
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def check_config_scope(role: Role, scope: str) -> bool:
    """配置页的子模块级检查（v1 只有主管需要区分）。

    主管仅在 `scope == "weight"` 时通过；管理员全通过。
    """
    if role is Role.ADMIN:
        return True
    if role is Role.SUPERVISOR:
        return scope in SUPERVISOR_CONFIG_SCOPES
    return False
