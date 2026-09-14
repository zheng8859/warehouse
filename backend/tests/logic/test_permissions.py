"""权限矩阵的契约测试（tasks.md §8 的验证）。

事实来源：13-权限分级与访问控制系统 §2.1（17 个 `resource.action` 标识）、
          §2.2（完整矩阵，逐格）、§2.3（四条角色摘要）、§6.2（第 2 层检查的判定式）
          26 附录B（**不作为**取值依据，见 §8.2 对 `outbound.operate` 的说明）
          openspec/changes/data-model-permission/tasks.md 8.1 ~ 8.4
          `26` 完成标准 #3（4 角色 × 10 资源全组合）、#5（`AUTO_ONLY` 封闭性）

## 这里测的是**数据**，不是行为

D9：端点级资源鉴权不在本阶段。所以本文件的断言对象是那张矩阵本身 ——
「4 角色 × 21 权限 = 84 格，每格与 13 §2.2 逐条一致」。它值钱的地方恰恰在于
**没有端点消费它**：矩阵是目标模型（13 §二 开头逐字），阶段七前端按它渲染菜单
（13 §3.1）、RBAC 落地后由后端强制（M5）。矩阵错了，错的是一整条链上的所有下游，
而在没有端点的时候，只有这条测试能发现。

## 逐格抄录，不做「摘要式」断言

表按 13 §2.2 的**行**抄（行序即文档行序），每行四格按文档列序（仓管员 / 计划员 /
主管 / 管理员）。刻意不写成「仓管员有哪些权限」的集合形式：集合形式下，
把 `inbound.operate` 误抄进计划员只是多一个元素，一眼看不出来；逐格抄录时，
错的那一格与文档那一行并排，审阅时能逐条对回去。

**🔧 在这张表里记 `True`**：13 §2.2 用 🔧 表示「主管有权限、但仅权重子模块」。
那是**两个维度** —— `check`（有没有这个权限）与 `check_config_scope`（限哪个子模块）。
两者合起来才是 🔧；硬把 🔧 折成一个布尔，就必须在 `check` 里塞进「子模块」这个
它拿不到的参数。§8.4 单独测范围那一维。
"""
from __future__ import annotations

import itertools

import pytest

from app.api.permissions import (
    AUTO_ONLY,
    CONFIG_SCOPES,
    ROLE_PERMISSIONS,
    SUPERVISOR_CONFIG_SCOPES,
    Permission,
    check,
    check_config_scope,
)
from app.core.enums import Role

pytestmark = pytest.mark.logic

_T, _F = True, False

ROLES: tuple[Role, ...] = (Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR, Role.ADMIN)

#: 13 §2.2 完整矩阵逐行抄录。列序：仓管员 / 计划员 / 主管 / 管理员。
#: 每行末尾的括注是文档该行的原样取值（含 ✅ / ❌ / 🔍 / 🔧 / 自动）。
MATRIX: tuple[tuple[Permission, bool, bool, bool, bool], ...] = (
    (Permission.ACCOUNT_MANAGE,       _F, _F, _F, _T),  # ❌ ❌ ❌ ✅
    (Permission.DATA_IMPORT_VIEW,     _T, _T, _F, _T),  # ✅ 🔍 ❌ ✅
    (Permission.DATA_IMPORT_OPERATE,  _T, _F, _F, _T),  # ✅ ❌ ❌ ✅
    (Permission.INBOUND_VIEW,         _T, _F, _F, _T),  # ✅ ❌ ❌ ✅
    (Permission.INBOUND_OPERATE,      _T, _F, _F, _T),  # ✅ ❌ ❌ ✅
    (Permission.OUTBOUND_VIEW,        _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.OUTBOUND_OPERATE,     _T, _F, _F, _T),  # ✅ ❌ ❌ ✅
    (Permission.RELOCATE_VIEW,        _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.RELOCATE_OPERATE,     _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.KPI_VIEW,             _T, _T, _T, _T),  # ✅ ✅ ✅ ✅
    (Permission.CONFIG_VIEW,          _F, _F, _T, _T),  # ❌ ❌ 🔧 ✅
    (Permission.CONFIG_CONFIGURE,     _F, _F, _T, _T),  # ❌ ❌ 🔧 ✅
    (Permission.CONVERSATION_VIEW,    _T, _T, _T, _T),  # ✅ ✅ ✅ ✅
    (Permission.CONVERSATION_OPERATE, _T, _T, _T, _T),  # ✅ ✅ ✅ ✅
    (Permission.LEDGER_VIEW,          _T, _T, _T, _T),  # ✅ ✅ ✅ ✅
    # 下两行文档写「自动」= 由作业流 / 引擎执行，**不向角色暴露手动入口**：
    # 在 `check` 这一维上四个角色全 False（`AUTO_ONLY` 恒 False），见 §8.3。
    (Permission.LEDGER_WRITE,         _F, _F, _F, _F),  # 自动
    (Permission.ENGINE_INVOKE,        _F, _F, _F, _F),  # 自动
    # 冷路径 `ai.*`（D6）：前三者仓管员/主管/管理员，计划员无；toggle 仅管理员。
    (Permission.AI_ASSIST,            _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.AI_WEIGHT_UPDATE,     _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.AI_RELOCATE_PROPOSE,  _T, _F, _T, _T),  # ✅ ❌ ✅ ✅
    (Permission.AI_TOGGLE,            _F, _F, _F, _T),  # ❌ ❌ ❌ ✅
)

#: 8.1 的「11 资源」—— 21 个标识背后的资源数，取自 13 §2.1 的表 + 冷路径 `ai`（29）。
RESOURCES: frozenset[str] = frozenset(
    {"account", "data_import", "inbound", "outbound", "relocate", "kpi",
     "config", "conversation", "ledger", "engine", "ai"}
)


# ------------------------------------------------------------------ 8.1 全组合


def test_matrix_covers_every_identifier_exactly_once() -> None:
    """21 行 —— 与 `Permission` 的成员数、13 §2.1 的表 + 冷路径 `ai.*` 逐条对上。

    只有「行数相等」挡不住「一个重复、另一个漏掉」，故断言两侧的**集合**相等。
    """
    assert len(Permission) == 21, "13 §2.1 + 29 号定义 21 个 resource.action 标识"
    assert [row[0] for row in MATRIX].__len__() == 21
    assert {row[0] for row in MATRIX} == set(Permission)


def test_identifiers_cover_the_eleven_resources() -> None:
    """资源的**前缀**恰好是 13 §2.1 的 10 个 + 冷路径 `ai` —— `kpi`/`engine`/`account`
    只有一个动作。

    这条与上一条互补：上一条防「标识本身抄漏」，这条防「标识写错成别的资源」
    （例如把 `outbound.view` 写成 `outbound.view_`、或凭空多一个 `report.view`）。
    """
    prefixes = {p.value.split(".")[0] for p in Permission}

    assert prefixes == RESOURCES


@pytest.mark.parametrize(
    ("permission", "role", "expected"),
    [(p, r, row[1 + i]) for row in MATRIX for i, r in enumerate(ROLES) for p in (row[0],)],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_every_role_permission_pair_matches_doc_13_section_2_2(
    permission: Permission, role: Role, expected: bool
) -> None:
    """8.1 的验证动作：84 格逐格与 13 §2.2 一致（`26` 完成标准 #3）。"""
    assert check(role, permission) is expected


def test_role_sets_match_the_matrix_columns() -> None:
    """`ROLE_PERMISSIONS` 与矩阵列**互为反演** —— 从矩阵反推的集合必须与实现完全一致。

    上一条按 `check()` 逐格比，这一条比**底层数据**：两者同时通过才能排除
    「`check` 里另有一套逻辑，恰好与矩阵一致而数据不一致」（例如 `check` 里
    硬编码了白名单、而 `ROLE_PERMISSIONS` 已经漂移）。
    """
    for index, role in enumerate(ROLES):
        derived = frozenset(row[0] for row in MATRIX if row[1 + index])
        assert ROLE_PERMISSIONS[role] == derived - AUTO_ONLY, role.value


def test_matrix_has_no_role_without_an_entry() -> None:
    """四个角色都要在 `ROLE_PERMISSIONS` 里有键 —— 少一个会让 `check` 静默全 False。

    `check` 用的 `.get(role, frozenset())` 是 fail-closed 的写法（安全方向对），
    但也意味着「漏了一个角色」不会报错、只会让那个角色的菜单全空。
    """
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_check_is_deterministic() -> None:
    """同样输入必得同样输出（红线之一）—— 全 84 格比两遍。"""
    for role, permission in itertools.product(Role, Permission):
        assert check(role, permission) == check(role, permission)


# ------------------------------------------------------------------ 8.2 边界用例

def test_supervisor_has_no_inbound_view() -> None:
    """8.2 点名之一：主管**没有** `inbound.view`。

    这条是最容易被「顺手补全」的一格：主管看起来"应该"能看所有作业。13 §2.2/§2.3
    的理由是分工（入库执行不归主管）—— 用户画像里的主管是「决策与监督，非落位」。
    """
    assert check(Role.SUPERVISOR, Permission.INBOUND_VIEW) is False
    assert check(Role.SUPERVISOR, Permission.INBOUND_OPERATE) is False


def test_supervisor_has_outbound_view_but_not_operate() -> None:
    """8.2 点名之二：主管**有** `outbound.view`、**无** `outbound.operate`。

    **以 13 §2.2 为准，不是 26 附录B** —— 附录B 的那一格与 §2.2 不一致（已登记
    tasks.md 9.4 的勘误）。这条断言就是那次裁决的落点：若有人按附录B 改代码，
    这里会红，改动就得先回答「以哪份文档为准」。
    """
    assert check(Role.SUPERVISOR, Permission.OUTBOUND_VIEW) is True
    assert check(Role.SUPERVISOR, Permission.OUTBOUND_OPERATE) is False


def test_planner_has_no_operate_on_any_of_the_three_jobs() -> None:
    """8.2 点名之三：计划员三类作业的 `operate` 全无（协调者，非执行者）。

    计划员是**唯一**一个只读角色的业务岗（13 §2.3），而「只读」在这里的准确含义是
    「没有任何 `operate`」：它仍有 `conversation.operate`（指令发起）—— 指令走确认卡，
    不是直接写台账。故这条只断言三类作业。
    """
    for permission in (
        Permission.INBOUND_OPERATE,
        Permission.OUTBOUND_OPERATE,
        Permission.RELOCATE_OPERATE,
    ):
        assert check(Role.PLANNER, permission) is False, permission.value

    # 但 view 也一并没有（计划员连作业页都进不去，13 §3.1）。
    for permission in (
        Permission.INBOUND_VIEW,
        Permission.OUTBOUND_VIEW,
        Permission.RELOCATE_VIEW,
    ):
        assert check(Role.PLANNER, permission) is False, permission.value


def test_planner_keeps_the_read_only_import_view() -> None:
    """计划员的数据导入格是 🔍 —— 在 `check` 这一维上是 `True`。

    「只读/查看」表示**看得到**（导入结果），与 `operate` 分开；把 🔍 当成 ❌ 是
    一种很自然的误读，而它的后果是计划员看不到数据是否导入成功 —— 而它恰恰是
    计划员协调排产的输入。
    """
    assert check(Role.PLANNER, Permission.DATA_IMPORT_VIEW) is True
    assert check(Role.PLANNER, Permission.DATA_IMPORT_OPERATE) is False


def test_warehouse_keeper_owns_the_three_jobs() -> None:
    """仓管员三类作业 view + operate 全有（13 §2.3：现场执行者）。"""
    for permission in (
        Permission.INBOUND_VIEW, Permission.INBOUND_OPERATE,
        Permission.OUTBOUND_VIEW, Permission.OUTBOUND_OPERATE,
        Permission.RELOCATE_VIEW, Permission.RELOCATE_OPERATE,
    ):
        assert check(Role.WAREHOUSE_KEEPER, permission) is True, permission.value


def test_warehouse_keeper_has_no_config_and_no_account() -> None:
    """仓管员不碰配置与账号（13 §2.3 末句「configure/manage 全 ❌」）。"""
    for permission in (
        Permission.CONFIG_VIEW, Permission.CONFIG_CONFIGURE, Permission.ACCOUNT_MANAGE,
    ):
        assert check(Role.WAREHOUSE_KEEPER, permission) is False, permission.value


def test_only_admin_manages_accounts() -> None:
    """`account.manage` 是**单角色**权限（13 §5.3：仅 admin）。

    单独写一条是因为它是 spec `auth`「账号由管理员开通」场景「非管理员被拒」的
    判据来源 —— 那条场景要 403，而 403 的判定就是这一格。
    """
    holders = [role for role in Role if check(role, Permission.ACCOUNT_MANAGE)]

    assert holders == [Role.ADMIN]


def test_admin_is_a_superset_of_everyone_except_auto_only() -> None:
    """管理员是**跨角色兜底**（13 §2.3）：其它三个角色的权限它全有。

    这条是矩阵的**结构性质**，不是文档原话 —— 但文档说管理员「拥有所有页面的全部
    操作权限（含账号管理、配置变更、业务执行），作为跨角色兜底与管理入口」。
    若哪天给某个角色加了权限却忘了管理员，这里会红。
    """
    for role in (Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR):
        assert ROLE_PERMISSIONS[role] <= ROLE_PERMISSIONS[Role.ADMIN], role.value


# ------------------------------------------------------------------ 8.2b 冷路径 ai.* 权限（D6）

def test_planner_has_no_ai_permissions() -> None:
    """spec `permission` 场景「计划员无 ai 建议权限」：计划员对四个 `ai.*` 全无。

    计划员是协调者（13 §2.3 的只读业务岗），冷路径建议是给执行/决策岗的辅助，
    不向它开放（D6）。
    """
    for permission in (
        Permission.AI_ASSIST,
        Permission.AI_WEIGHT_UPDATE,
        Permission.AI_RELOCATE_PROPOSE,
        Permission.AI_TOGGLE,
    ):
        assert check(Role.PLANNER, permission) is False, permission.value


def test_ai_toggle_is_admin_only() -> None:
    """spec `permission` 场景「ai.toggle 仅管理员」：非管理员 403 那一格的判据来源。"""
    holders = [role for role in Role if check(role, Permission.AI_TOGGLE)]
    assert holders == [Role.ADMIN]


def test_ai_assist_grants_read_suggestions_to_three_roles() -> None:
    """`ai.assist` 授予仓管员/主管/管理员，计划员无（D6）。"""
    for role in (Role.WAREHOUSE_KEEPER, Role.SUPERVISOR, Role.ADMIN):
        assert check(role, Permission.AI_ASSIST) is True, role.value
    assert check(Role.PLANNER, Permission.AI_ASSIST) is False


# ------------------------------------------------------------------ 8.3 AUTO_ONLY 封闭性

def test_auto_only_is_exactly_the_two_red_line_permissions() -> None:
    """`AUTO_ONLY` 恰好是 `ledger.write` 与 `engine.invoke`（13 §2.2 的「自动」）。"""
    assert AUTO_ONLY == {Permission.LEDGER_WRITE, Permission.ENGINE_INVOKE}


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
def test_auto_only_permissions_are_never_granted(role: Role) -> None:
    """8.3 的验证动作：`ledger.write` 与 `engine.invoke` 对四角色**恒为 False**
    （`26` 完成标准 #5）。

    红线原话（CLAUDE.md 第四节）：台账由引擎 / 作业流自动写，这两个权限
    **不向任何角色开放手动入口**。故这里逐角色断言，而不是断言「不在集合里」——
    后者挡不住有人在 `check` 里为这两个值开一条特例分支（例如「调试用」）。
    """
    for permission in AUTO_ONLY:
        assert check(role, permission) is False, f"{role.value} 不该有 {permission.value}"


def test_auto_only_never_appears_in_any_role_set() -> None:
    """连**数据**里也不许出现（`check` 之外的入口：`ROLE_PERMISSIONS` 的读方）。

    前端菜单过滤与将来的 RBAC 强制若直接读 `ROLE_PERMISSIONS` 而绕开 `check`，
    这里就是那道防线。
    """
    for role, permissions in ROLE_PERMISSIONS.items():
        assert not (permissions & AUTO_ONLY), role.value


def test_non_enum_arguments_are_rejected_loudly() -> None:
    """裸字符串必须**报类型错**，不能静默返回 False。

    与两个状态机同一处陷阱：`Permission` 与 `Role` 都是 `str` 枚举，
    `check("admin", "kpi.view")` 看着完全合理（值比较都能过），但集合与字典查的是
    `hash`，而 `Enum.__hash__` 取**成员名** —— 于是它不会报错，只会一路返回 False，
    表现为「管理员登录后菜单全空」。fail-closed 的方向是对的，方向对不代表不是事故：
    现场看到的是"权限突然都没了"，而日志里一句错都没有。
    """
    with pytest.raises(TypeError):
        check("admin", Permission.KPI_VIEW)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        check(Role.ADMIN, "kpi.view")  # type: ignore[arg-type]


# ------------------------------------------------------------------ 8.4 配置子模块受限

def test_config_scopes_are_the_five_sub_modules() -> None:
    """8.4：配置页 5 个子模块（22 §2.1 ⑦：导入模板 / cap 口径 / 因子权重 /
    近站台预留比例 / 对话指令词），且主管的范围是它的真子集。"""
    assert len(CONFIG_SCOPES) == 5
    assert SUPERVISOR_CONFIG_SCOPES < set(CONFIG_SCOPES)
    assert SUPERVISOR_CONFIG_SCOPES == {"weight"}


@pytest.mark.parametrize("scope", sorted(CONFIG_SCOPES))
def test_supervisor_may_only_configure_weights(scope: str) -> None:
    """8.4 的验证动作：主管仅 `weight` 通过，其余 4 个子模块拒绝（13 §2.2 的 🔧）。"""
    assert check_config_scope(Role.SUPERVISOR, scope) is (scope == "weight")


@pytest.mark.parametrize("scope", sorted(CONFIG_SCOPES))
def test_keeper_and_planner_have_no_config_scope_at_all(scope: str) -> None:
    """仓管员与计划员对**任何**子模块都不通过 —— 它们连 `config.view` 都没有。"""
    assert check_config_scope(Role.WAREHOUSE_KEEPER, scope) is False
    assert check_config_scope(Role.PLANNER, scope) is False


@pytest.mark.parametrize("scope", sorted(CONFIG_SCOPES))
def test_admin_may_configure_every_scope(scope: str) -> None:
    """管理员全通过（13 §2.3：配置变更）—— 5 个子模块逐个断言。"""
    assert check_config_scope(Role.ADMIN, scope) is True


def test_unknown_config_scope_is_rejected_for_everyone_but_admin() -> None:
    """未知子模块名：主管/仓管员/计划员一律拒绝，**管理员仍然通过**。

    管理员的检查是「无条件的 True」而不是「scope 在 CONFIG_SCOPES 里」——
    这是刻意的：管理员是跨角色兜底（13 §2.3），若把范围表也套到它身上，
    那么将来新增第六个子模块时，管理员会在**新子模块上线的那一刻**失去它，
    而那张范围表未必同时更新。
    """
    for role in (Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR):
        assert check_config_scope(role, "not_a_scope") is False, role.value

    assert check_config_scope(Role.ADMIN, "not_a_scope") is True


def test_scope_check_rejects_bare_strings() -> None:
    """`check_config_scope` 的角色入参同样要报类型错（同 `check`）。"""
    with pytest.raises(TypeError):
        check_config_scope("supervisor", "weight")  # type: ignore[arg-type]
