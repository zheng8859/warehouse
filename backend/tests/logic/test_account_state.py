"""账号状态机的契约测试（tasks.md 7.3 的验证）。

事实来源：13-权限分级与访问控制系统 §5.1（状态流转图）、§5.2（账号数据模型）
          spec `auth`「账号状态迁移」（四条迁移，逐字）
          design.md D2（`AccountStatus` 补 `REJECTED`）

状态机是**纯函数**（与 `app/core/state_machine.py` 同一处置，无 IO）：本文件不建库、
不用 session。逻辑测试保留完整代码 —— 4 个状态共 16 个有序对，合法的只有 4 个，
逐对穷举等于把 spec 那段话逐条抄成断言。将来有人「顺手」让 `rejected` 也能激活，
红的是这条用例，而不是生产上某天一个被驳回的申请自己活了。

**与作业单状态机（`test_job_state.py`）的两处刻意差别**，都是文档决定的：

  - 这里**没有合法自环**。`PENDING → PENDING` 在作业单上是「批量分配失败可重试」
    （15 §3.1），而 spec `auth` 的四条迁移里没有自环。
  - 终态是 `rejected`，不是 `disabled` —— `disabled → active` 明确存在（停用可重新启用），
    故「停用」不是终态；而 `rejected` 无出边，它对应「这条申请到此为止」。

**本阶段没有消费者**：账号管理的写端点（管理员创建 / 激活 / 停用 / 驳回）属路线图
（13 §5.1 的权限动作，与 D9 的「端点级资源鉴权不在本阶段」同批）。本模块与 §8 的权限
矩阵一样，本阶段交付的是**数据 + 纯判定函数 + 全组合断言**，端点落地时直接复用这张表。
"""
from __future__ import annotations

import ast
import itertools
import pathlib

import pytest

from app.core import account_state
from app.core.account_state import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
)
from app.core.enums import AccountStatus
from app.core.errors import StateConflict

pytestmark = pytest.mark.logic

#: spec `auth`「账号状态迁移」逐字列出的四条，顺序与那句话一致。
LEGAL_PAIRS: tuple[tuple[AccountStatus, AccountStatus], ...] = (
    (AccountStatus.PENDING, AccountStatus.ACTIVE),      # 管理员激活
    (AccountStatus.PENDING, AccountStatus.REJECTED),    # 管理员驳回申请
    (AccountStatus.ACTIVE, AccountStatus.DISABLED),     # 停用（紧急吊销，13 §8.3）
    (AccountStatus.DISABLED, AccountStatus.ACTIVE),     # 重新启用
)

ALL_PAIRS: tuple[tuple[AccountStatus, AccountStatus], ...] = tuple(
    itertools.product(AccountStatus, repeat=2)
)

ILLEGAL_PAIRS: tuple[tuple[AccountStatus, AccountStatus], ...] = tuple(
    pair for pair in ALL_PAIRS if pair not in LEGAL_PAIRS
)

#: 4 条合法、12 条非法 —— 写死个数，改动迁移表必须同时改这里（有意的摩擦）。
_EXPECTED_LEGAL_COUNT = 4


def _pair_id(pair: tuple[AccountStatus, AccountStatus]) -> str:
    return f"{pair[0].value}_to_{pair[1].value}"


# ------------------------------------------------------------------ 迁移表本身

def test_transition_table_has_exactly_the_four_legal_pairs() -> None:
    """表的全部内容 == spec 那四条，多一条少一条都算错（`diagnosed` 之类的第五值一并挡住）。"""
    actual = {
        (current, target)
        for current, targets in LEGAL_TRANSITIONS.items()
        for target in targets
    }
    assert actual == set(LEGAL_PAIRS)
    assert sum(len(targets) for targets in LEGAL_TRANSITIONS.values()) == _EXPECTED_LEGAL_COUNT


def test_illegal_pair_count_is_the_complement() -> None:
    """16 − 4 = 12。让「合法集悄悄变大」无法发生 —— 加一条边必然改这里的数字。"""
    assert len(ALL_PAIRS) == len(AccountStatus) ** 2
    assert len(ILLEGAL_PAIRS) == len(AccountStatus) ** 2 - _EXPECTED_LEGAL_COUNT


def test_every_status_is_a_key() -> None:
    """四个状态都必须出现在表里（终态值为空集）—— 缺键会把「没这条迁移」误报成 `KeyError`。"""
    assert set(LEGAL_TRANSITIONS) == set(AccountStatus)
    assert set(AccountStatus) == {
        AccountStatus.PENDING, AccountStatus.ACTIVE,
        AccountStatus.DISABLED, AccountStatus.REJECTED,
    }, "四值取自 spec `auth`；17 §6.1 散文只写三个（缺 rejected），以 §九 与 D2 为准"


# ------------------------------------------------------------------ 逐对穷举

@pytest.mark.parametrize("pair", LEGAL_PAIRS, ids=_pair_id)
def test_legal_transition_is_accepted(pair: tuple[AccountStatus, AccountStatus]) -> None:
    """四条合法迁移全部通过。"""
    current, target = pair
    assert can_transition(current, target) is True
    assert assert_transition(current, target) is target


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=_pair_id)
def test_illegal_transition_is_rejected(pair: tuple[AccountStatus, AccountStatus]) -> None:
    """未列出的 12 条被拒，且 `detail` 带上当前态与目标态（供前端提示）。"""
    current, target = pair
    assert can_transition(current, target) is False
    with pytest.raises(StateConflict) as excinfo:
        assert_transition(current, target)
    assert excinfo.value.detail == {"current": current.value, "target": target.value}


def test_rejected_cannot_be_activated() -> None:
    """tasks 7.3 点名的一例：`rejected → active` 被拒（spec 场景「已驳回账号不得直接激活」）。

    单写一遍不是冗余。它挡住的是最自然的一种"顺手"：既然 `disabled → active` 是
    「重新启用」，把已驳回的申请也一并激活看起来同样合理 —— 但那样驳回就只是
    一个装饰性动作，申请人的账号会跳过管理员的复评直接可用。要恢复，只能重新提交
    申请（走一次新的 `pending`），不是在这张图上加一条边。
    """
    assert can_transition(AccountStatus.REJECTED, AccountStatus.ACTIVE) is False
    with pytest.raises(StateConflict):
        assert_transition(AccountStatus.REJECTED, AccountStatus.ACTIVE)


def test_rejected_is_terminal() -> None:
    """`rejected` 没有任何出边 —— 四个目标逐个断言，含自环。"""
    assert LEGAL_TRANSITIONS[AccountStatus.REJECTED] == frozenset()
    assert AccountStatus.REJECTED in TERMINAL_STATUSES

    for target in AccountStatus:
        assert can_transition(AccountStatus.REJECTED, target) is False


def test_disabled_is_not_terminal_but_active_is_a_hub() -> None:
    """`disabled` **不是**终态（停用可重新启用，spec 场景「停用后可重新启用」）。

    终态集只有 `rejected` —— 若有人按"终态 = 不能登录"来理解，`disabled` 会被误判成终态，
    于是「重新启用」这条路径就没了。判据是**图上的出边**，不是业务语义。
    """
    assert TERMINAL_STATUSES == {AccountStatus.REJECTED}
    assert LEGAL_TRANSITIONS[AccountStatus.DISABLED] == frozenset({AccountStatus.ACTIVE})

    # active 是唯一有两个出边的状态：停用、以及（无 —— 只有停用）。逐条写清楚：
    assert LEGAL_TRANSITIONS[AccountStatus.ACTIVE] == frozenset({AccountStatus.DISABLED})
    assert LEGAL_TRANSITIONS[AccountStatus.PENDING] == frozenset(
        {AccountStatus.ACTIVE, AccountStatus.REJECTED}
    )


def test_no_self_loop_is_legal() -> None:
    """四个自环全部非法 —— 与作业单状态机（`PENDING → PENDING` 合法）形成对照。

    自环在这里没有任何文档依据，而它一旦被"顺手"放行，「重复点一次激活」就会
    在 `last_login_at` / 审计链上留下第二条记录。
    """
    for status in AccountStatus:
        assert can_transition(status, status) is False, status


def test_cannot_skip_activation() -> None:
    """`pending` 只能到 `active` / `rejected`：不能直接到 `disabled`。

    这条挡的是「先建号停用着，回头再启用」这种走法 —— spec 的状态图里没有它，
    而它会让「账号已开通但不可用」成为一个合法状态，看板上分不清"待激活"与"已停用"。
    """
    assert can_transition(AccountStatus.PENDING, AccountStatus.DISABLED) is False
    assert can_transition(AccountStatus.ACTIVE, AccountStatus.PENDING) is False
    assert can_transition(AccountStatus.ACTIVE, AccountStatus.REJECTED) is False


# ------------------------------------------------------------------ 纯函数性质

def test_statuses_must_be_account_status_instances() -> None:
    """传字符串必须**报类型错**，不能静默判成「非法迁移」。

    在 `AccountStatus` 上这个陷阱比 `JobStatus` 更隐蔽：两个取值都是 lowercase，
    `AccountStatus.ACTIVE == "active"` 为真（str 混合枚举按值比较），于是
    `if status == "active"` 这种写法看起来完全正确；但查表走 `hash`，
    而 `Enum.__hash__` 取的是**成员名** `ACTIVE` —— 拿 `"active"` 查表必然 `KeyError`。
    「值比较通过、哈希查表失败」是同一份数据的两套语义，只能在入口处拒绝。
    """
    with pytest.raises(TypeError):
        can_transition("active", AccountStatus.DISABLED)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        assert_transition(AccountStatus.PENDING, "active")  # type: ignore[arg-type]

    # 大写成员名同样不接受 —— 它连值比较都不过，更不该在这里被猜成合法。
    with pytest.raises(TypeError):
        assert_transition(AccountStatus.PENDING, "ACTIVE")  # type: ignore[arg-type]


def test_can_transition_is_deterministic() -> None:
    """同样输入必得同样输出（红线：本地确定性计算）—— 全 16 对逐个比。"""
    for pair in ALL_PAIRS:
        assert can_transition(*pair) == can_transition(*pair)


def test_assert_transition_has_no_side_effects() -> None:
    """重复调用不累积状态：合法迁移调两次，两次都通过。"""
    for pair in LEGAL_PAIRS:
        assert assert_transition(*pair) is pair[1]
        assert assert_transition(*pair) is pair[1]


#: 本模块允许出现的全部 import 来源。多一个都要在这里显式加 —— 加的时候会看见
#: 「我在往一个要求无 IO 的模块里塞依赖」。
_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "enum",
    "types",  # MappingProxyType —— 让迁移表只读，不是 IO
    "typing",
    "app.core.enums",
    "app.core.errors",
}


def test_module_imports_no_io_dependencies() -> None:
    """与作业单状态机同一处置：用 AST 读 import 清单，而不是「看代码觉得没 IO」。

    挡的是有人在状态机里 `from sqlalchemy.orm import Session` 去「顺手查一下库里那行」
    —— 那一刻它就不再是纯函数，确定性会变成「取决于当时库里的状态」。
    """
    source = pathlib.Path(account_state.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= _ALLOWED_IMPORTS, f"状态机引入了不在白名单内的依赖：{imported - _ALLOWED_IMPORTS}"
