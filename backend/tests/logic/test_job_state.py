"""JobOrder 状态机的契约测试（tasks.md 4.2 的验证）。

事实来源：15-入库出库移库与后验流程设计 §3.1（状态机图，15 条迁移）、§11.7（重复执行与幂等）
          spec `data-model`「JobOrder 状态机」

状态机是**纯函数**（tasks.md 4.2 要求「无 IO」）：本文件不建库、不用 session，全部用例只调
`app/core/state_machine.py`。好处不只是快 —— 「同样输入必得同样输出」这条确定性要求，
在无 IO 的模块上可以直接断言（见 `test_can_transition_is_deterministic`）。

**为什么这里值得逐对穷举**：10 个状态共 100 个有序对，合法的只有 15 个。逐对穷举等于把
15 §3.1 的图逐条抄成断言 —— 将来有人「顺手」放宽一条（比如让 `EXECUTED` 能回到 `PLANNED`），
红的是这条用例，而不是生产上某天的两条台账。
"""
from __future__ import annotations

import ast
import itertools
import pathlib

import pytest

from app.core import state_machine
from app.core.enums import JobStatus
from app.core.errors import StateConflict
from app.core.state_machine import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    assert_transition,
    can_transition,
)

pytestmark = pytest.mark.logic

#: 15 §3.1 状态机图里的 15 条迁移，**逐条抄录**，顺序与图一致。
#: 注释里的括注就是图上的说明文字 —— 它们是这些边的存在理由。
LEGAL_PAIRS: tuple[tuple[JobStatus, JobStatus], ...] = (
    (JobStatus.PENDING, JobStatus.PLANNED),      # 批量分配/生成方案成功
    (JobStatus.PENDING, JobStatus.PENDING),      # 批量分配失败可重试
    (JobStatus.PENDING, JobStatus.CANCELLED),    # 移出本次批量
    (JobStatus.PLANNED, JobStatus.CONFIRMED),    # 操作员确认（二次确认卡）
    (JobStatus.PLANNED, JobStatus.REJECTED),     # 操作员驳回
    (JobStatus.PLANNED, JobStatus.CANCELLED),    # 移出本次批量
    (JobStatus.REJECTED, JobStatus.PENDING),     # 重新入队（可再次分配）
    (JobStatus.CONFIRMED, JobStatus.EXECUTED),   # 写台账成功
    (JobStatus.CONFIRMED, JobStatus.PLANNED),    # 写台账失败/回滚
    (JobStatus.EXECUTED, JobStatus.VERIFYING),   # 台账写入成功后自动触发后验
    (JobStatus.EXECUTED, JobStatus.VOID),        # 冲正
    (JobStatus.VERIFYING, JobStatus.VERIFIED),   # 后验完成（达标/偏离由 Verification 标记）
    (JobStatus.VERIFYING, JobStatus.VERIFY_FAILED),  # 后验失败/超时
    (JobStatus.VERIFY_FAILED, JobStatus.VERIFYING),  # 重试后验
    (JobStatus.VERIFIED, JobStatus.VOID),        # 冲正
)

ALL_PAIRS: tuple[tuple[JobStatus, JobStatus], ...] = tuple(
    itertools.product(JobStatus, repeat=2)
)

ILLEGAL_PAIRS: tuple[tuple[JobStatus, JobStatus], ...] = tuple(
    pair for pair in ALL_PAIRS if pair not in LEGAL_PAIRS
)

#: 15 条合法、85 条非法 —— 写死个数，改动迁移表必须同时改这里（有意的摩擦）。
_EXPECTED_LEGAL_COUNT = 15


def _pair_id(pair: tuple[JobStatus, JobStatus]) -> str:
    return f"{pair[0].value}_to_{pair[1].value}"


# ------------------------------------------------------------------ 迁移表本身

def test_transition_table_has_exactly_the_ten_legal_pairs() -> None:
    """表的全部内容 == 15 §3.1 的 15 条，多一条少一条都算错。"""
    actual = {
        (current, target)
        for current, targets in LEGAL_TRANSITIONS.items()
        for target in targets
    }
    assert actual == set(LEGAL_PAIRS)
    assert sum(len(targets) for targets in LEGAL_TRANSITIONS.values()) == _EXPECTED_LEGAL_COUNT


def test_illegal_pair_count_is_the_complement() -> None:
    """100 − 15 = 85。这条断言的作用是让「合法集缩小」无法悄悄发生。"""
    assert len(ALL_PAIRS) == len(JobStatus) ** 2
    assert len(ILLEGAL_PAIRS) == len(JobStatus) ** 2 - _EXPECTED_LEGAL_COUNT


def test_every_status_is_a_key() -> None:
    """终态也必须出现在表里（值为空集）—— 缺键会让 `LEGAL_TRANSITIONS[VERIFIED]`
    抛 `KeyError`，把「没这条迁移」误报成崩溃。"""
    assert set(LEGAL_TRANSITIONS) == set(JobStatus)


def test_transitions_only_point_at_known_statuses() -> None:
    """迁移目标必须是 10 个状态之一（枚举类型本身保证了，这里是回归护栏）。"""
    for targets in LEGAL_TRANSITIONS.values():
        assert set(targets) <= set(JobStatus)


# ------------------------------------------------------------------ 逐对穷举

@pytest.mark.parametrize("pair", LEGAL_PAIRS, ids=_pair_id)
def test_legal_transition_is_accepted(pair: tuple[JobStatus, JobStatus]) -> None:
    """tasks 4.2 的验证动作之一：全部 15 条合法迁移通过。"""
    current, target = pair
    assert can_transition(current, target) is True
    assert assert_transition(current, target) is target


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=_pair_id)
def test_illegal_transition_is_rejected(pair: tuple[JobStatus, JobStatus]) -> None:
    """tasks 4.2 的验证动作之二：未列出的迁移被拒（`PENDING → EXECUTED` 是典型）。"""
    current, target = pair
    assert can_transition(current, target) is False
    with pytest.raises(StateConflict) as excinfo:
        assert_transition(current, target)
    assert excinfo.value.detail == {"current": current.value, "target": target.value}


def test_pending_to_executed_is_rejected() -> None:
    """spec 场景「未定义迁移被拒绝」逐字对应的一条（跳过 PLANNED / CONFIRMED 直接执行）。

    这条单独再写一遍不是冗余：它同时是红线「未确认不产生台账」的入口 ——
    没有 `PENDING → EXECUTED`，就不可能在未过二次确认卡时写出落位台账。
    """
    assert can_transition(JobStatus.PENDING, JobStatus.EXECUTED) is False
    with pytest.raises(StateConflict):
        assert_transition(JobStatus.PENDING, JobStatus.EXECUTED)


def test_pending_self_loop_is_allowed() -> None:
    """15 §3.1：「批量分配失败可重试」。自环是**唯一**允许的同状态迁移。"""
    assert can_transition(JobStatus.PENDING, JobStatus.PENDING) is True

    for status in JobStatus:
        if status is JobStatus.PENDING:
            continue
        assert can_transition(status, status) is False, status


def test_terminal_statuses_are_cancelled_and_void() -> None:
    """10 态下的终态是 `CANCELLED` 与 `VOID`，`VERIFIED` **不再是终态**（可被冲正拉回 `VOID`）。

    这是 10 态图的直接推论：`TERMINAL_STATUSES` 是「无出边」的推导值（非手写），
    一旦把 `VERIFIED → VOID` 加进迁移表，它就从终态集里自动退出。
    """
    assert LEGAL_TRANSITIONS[JobStatus.VERIFIED] == frozenset({JobStatus.VOID})
    assert LEGAL_TRANSITIONS[JobStatus.VOID] == frozenset()
    assert TERMINAL_STATUSES == {JobStatus.CANCELLED, JobStatus.VOID}

    # VOID 是终态：无出边、不可自环。
    for target in JobStatus:
        assert can_transition(JobStatus.VOID, target) is False
    assert can_transition(JobStatus.VOID, JobStatus.VOID) is False

    # VERIFIED 唯一的出边是冲正。
    assert can_transition(JobStatus.VERIFIED, JobStatus.VOID) is True
    for target in JobStatus:
        if target is JobStatus.VOID:
            continue
        assert can_transition(JobStatus.VERIFIED, target) is False, target


def test_gate_edges_are_directional() -> None:
    """两条最容易被「顺手放宽」的边，方向必须钉死：

    - `CONFIRMED → PLANNED` 有（15 §3.1「写台账失败/回滚」），但 `EXECUTED → PLANNED` 没有。
      台账一旦写成，回退会造出「单子回到待确认、台账却已存在」的状态（15 §11.7 的重复执行）。
    - `PENDING → PENDING` 有，但 `REJECTED → PLANNED` 没有：驳回后必须回 `PENDING` 重新走分配，
      否则会绕过批量分配直接复用旧方案。
    """
    assert can_transition(JobStatus.CONFIRMED, JobStatus.PLANNED) is True
    assert can_transition(JobStatus.EXECUTED, JobStatus.PLANNED) is False
    assert can_transition(JobStatus.EXECUTED, JobStatus.CONFIRMED) is False

    assert can_transition(JobStatus.REJECTED, JobStatus.PENDING) is True
    assert can_transition(JobStatus.REJECTED, JobStatus.PLANNED) is False
    assert can_transition(JobStatus.REJECTED, JobStatus.CONFIRMED) is False


def test_cancelled_is_terminal_but_rejected_is_not() -> None:
    """`CANCELLED` 是「移出本次批量」—— 本次批量内不再回来（要回来是一次新导入/新入队，
    不是状态机上的回边）。`REJECTED` 则明确有回边（15 §3.1）。"""
    assert LEGAL_TRANSITIONS[JobStatus.CANCELLED] == frozenset()
    assert LEGAL_TRANSITIONS[JobStatus.REJECTED] == frozenset({JobStatus.PENDING})


# ------------------------------------------------------------------ 纯函数性质

def test_statuses_must_be_job_status_instances() -> None:
    """传字符串必须**报类型错**，不能静默判成「非法迁移」。

    这一条不是洁癖。`JobStatus` 是 `str` 枚举：`JobStatus.PENDING == "PENDING"` 为真，
    但 `hash` 走的是成员名（`Enum.__hash__`），于是 `"PENDING" in 迁移表` 的结果取决于
    成员名与取值是否恰好同形 —— 今天同形所以能查到，改天有人把取值改成 lowercase，
    同一次调用就会从「合法」翻成「非法」，而报错信息说的是「状态冲突」，
    排查会一路查到并发上。**类型错了就说类型错了。**
    """
    with pytest.raises(TypeError):
        can_transition("PENDING", JobStatus.PLANNED)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        assert_transition(JobStatus.PENDING, "pending")  # type: ignore[arg-type]


def test_can_transition_is_deterministic() -> None:
    """同样输入必得同样输出（红线：核心链路本地确定性计算）。

    连同下面那条「不构造任何 IO 对象」一起，把 tasks 4.2 的「纯函数无 IO」变成可执行断言。
    """
    for pair in ALL_PAIRS:
        assert can_transition(*pair) == can_transition(*pair)


def test_assert_transition_does_not_raise_on_legal_pair_twice() -> None:
    """重复调用不累积状态（无副作用）—— 合法迁移调两次，两次都该通过。"""
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
    """tasks 4.2：状态机「纯函数无 IO」。

    用 AST 读自己的 import 清单，而不是「看代码觉得没 IO」—— 后者挡不住有人在
    状态机里 `from sqlalchemy.orm import Session` 去「顺手校验一下库里那行」，
    那一刻状态机就不再是纯函数，而确定性会变成「取决于当时库里的状态」。
    """
    source = pathlib.Path(state_machine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= _ALLOWED_IMPORTS, f"状态机引入了不在白名单内的依赖：{imported - _ALLOWED_IMPORTS}"
