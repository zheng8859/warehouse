"""ImportSession 状态机的契约测试（tasks.md 1.1 的验证）。

事实来源：16-数据衔接与 cap 自维护设计 §3.1（状态机图，10 条迁移）
          spec `data-model`「ImportSession 状态机」、spec `data-import`「数据时点标注与导入即基准」

状态机是**纯函数**（与 `test_job_state.py` 同构，tasks 1.1 要求「无 IO」）：本文件不建库、
不用 session，全部用例只调 `app/core/import_state.py`。

**为什么这里值得逐对穷举**：8 个状态共 64 个有序对，合法的只有 10 个。逐对穷举等于把
16 §3.1 的图逐条抄成断言 —— 将来有人「顺手」放宽一条（比如让 `FAILED` 能直接回到
`VALIDATING`，绕过「时点标注 + 文件重新上传」入口），红的是这条用例，而不是生产上
某天一条被静默重检的旧文件。
"""
from __future__ import annotations

import ast
import itertools
import pathlib

import pytest

from app.core import import_state
from app.core.enums import ImportStatus
from app.core.errors import StateConflict
from app.core.import_state import (
    IMPORT_TERMINAL_STATUSES,
    LEGAL_IMPORT_TRANSITIONS,
    assert_import_transition,
    can_import_transition,
)

pytestmark = pytest.mark.logic

#: 16 §3.1 状态机图里的 10 条迁移，**逐条抄录**，顺序与图一致。
#: 注释里的括注就是图上的说明文字 —— 它们是这些边的存在理由。
LEGAL_PAIRS: tuple[tuple[ImportStatus, ImportStatus], ...] = (
    (ImportStatus.DRAFT, ImportStatus.VALIDATING),      # 点击「开始校验」
    (ImportStatus.DRAFT, ImportStatus.DISCARDED),       # 用户取消
    (ImportStatus.VALIDATING, ImportStatus.VALIDATED),  # 校验全部通过
    (ImportStatus.VALIDATING, ImportStatus.FAILED),     # 字段未命中 / 口径异常 / 时点缺失
    (ImportStatus.FAILED, ImportStatus.DRAFT),          # 修正文件后重新校验（唯一回边）
    (ImportStatus.VALIDATED, ImportStatus.IMPORTING),   # 点击「执行导入」
    (ImportStatus.VALIDATED, ImportStatus.DISCARDED),   # 用户取消
    (ImportStatus.IMPORTING, ImportStatus.IMPORTED),    # 写入与分流成功
    (ImportStatus.IMPORTING, ImportStatus.FAILED),      # 写入异常 / 回滚
    (ImportStatus.IMPORTED, ImportStatus.BASELINE),     # 快照重算 cap 基线完成
)

ALL_PAIRS: tuple[tuple[ImportStatus, ImportStatus], ...] = tuple(
    itertools.product(ImportStatus, repeat=2)
)

ILLEGAL_PAIRS: tuple[tuple[ImportStatus, ImportStatus], ...] = tuple(
    pair for pair in ALL_PAIRS if pair not in LEGAL_PAIRS
)

#: 10 条合法、54 条非法 —— 写死个数，改动迁移表必须同时改这里（有意的摩擦）。
_EXPECTED_LEGAL_COUNT = 10


def _pair_id(pair: tuple[ImportStatus, ImportStatus]) -> str:
    return f"{pair[0].value}_to_{pair[1].value}"


# ------------------------------------------------------------------ 迁移表本身

def test_transition_table_has_exactly_the_ten_legal_pairs() -> None:
    """表的全部内容 == 16 §3.1 的 10 条，多一条少一条都算错。"""
    actual = {
        (current, target)
        for current, targets in LEGAL_IMPORT_TRANSITIONS.items()
        for target in targets
    }
    assert actual == set(LEGAL_PAIRS)
    assert sum(len(targets) for targets in LEGAL_IMPORT_TRANSITIONS.values()) == _EXPECTED_LEGAL_COUNT


def test_illegal_pair_count_is_the_complement() -> None:
    """64 − 10 = 54。这条断言的作用是让「合法集缩小」无法悄悄发生。"""
    assert len(ALL_PAIRS) == len(ImportStatus) ** 2
    assert len(ILLEGAL_PAIRS) == len(ImportStatus) ** 2 - _EXPECTED_LEGAL_COUNT


def test_every_status_is_a_key() -> None:
    """终态也必须出现在表里（值为空集）—— 缺键会让 `LEGAL_IMPORT_TRANSITIONS[BASELINE]`
    抛 `KeyError`，把「没这条迁移」误报成崩溃。"""
    assert set(LEGAL_IMPORT_TRANSITIONS) == set(ImportStatus)


def test_transitions_only_point_at_known_statuses() -> None:
    """迁移目标必须是 8 个状态之一（枚举类型本身保证了，这里是回归护栏）。"""
    for targets in LEGAL_IMPORT_TRANSITIONS.values():
        assert set(targets) <= set(ImportStatus)


# ------------------------------------------------------------------ 逐对穷举

@pytest.mark.parametrize("pair", LEGAL_PAIRS, ids=_pair_id)
def test_legal_transition_is_accepted(pair: tuple[ImportStatus, ImportStatus]) -> None:
    """tasks 1.1 的验证动作之一：全部 10 条合法迁移通过。"""
    current, target = pair
    assert can_import_transition(current, target) is True
    assert assert_import_transition(current, target) is target


@pytest.mark.parametrize("pair", ILLEGAL_PAIRS, ids=_pair_id)
def test_illegal_transition_is_rejected(pair: tuple[ImportStatus, ImportStatus]) -> None:
    """tasks 1.1 的验证动作之二：未列出的迁移被拒（`DRAFT → IMPORTING` 是典型）。"""
    current, target = pair
    assert can_import_transition(current, target) is False
    with pytest.raises(StateConflict) as excinfo:
        assert_import_transition(current, target)
    assert excinfo.value.detail == {"current": current.value, "target": target.value}


def test_draft_to_importing_is_rejected() -> None:
    """红线「未校验通过或未执行导入时不得提前分流」的状态机入口。

    没有 `DRAFT → IMPORTING` / `VALIDATING → IMPORTING`，就不可能在校验通过前把
    PO/DO/INV 分流成队列 / 任务 / 基线。
    """
    assert can_import_transition(ImportStatus.DRAFT, ImportStatus.IMPORTING) is False
    assert can_import_transition(ImportStatus.VALIDATING, ImportStatus.IMPORTING) is False
    assert can_import_transition(ImportStatus.VALIDATED, ImportStatus.IMPORTING) is True
    with pytest.raises(StateConflict):
        assert_import_transition(ImportStatus.DRAFT, ImportStatus.IMPORTING)


def test_failed_only_retries_through_draft() -> None:
    """`FAILED` 唯一的出边是 `DRAFT`（16 §3.1「修正文件后重新校验」）。

    `FAILED → VALIDATING` 的捷径必须不存在：那会绕过「时点标注 + 文件重新上传」入口，
    让一份已判失败的旧文件被静默重检。`FAILED → IMPORTING` 同样不存在 —— 校验不过
    就不可能进导入。
    """
    assert LEGAL_IMPORT_TRANSITIONS[ImportStatus.FAILED] == frozenset({ImportStatus.DRAFT})
    assert can_import_transition(ImportStatus.FAILED, ImportStatus.DRAFT) is True
    assert can_import_transition(ImportStatus.FAILED, ImportStatus.VALIDATING) is False
    assert can_import_transition(ImportStatus.FAILED, ImportStatus.IMPORTING) is False


def test_terminal_statuses_are_baseline_and_discarded() -> None:
    """8 态下的终态是 `BASELINE` 与 `DISCARDED`，都没有出边、都不可自环。

    `FAILED` **不是终态**：它可回 `DRAFT` 重新校验（见上一条）。
    """
    assert LEGAL_IMPORT_TRANSITIONS[ImportStatus.BASELINE] == frozenset()
    assert LEGAL_IMPORT_TRANSITIONS[ImportStatus.DISCARDED] == frozenset()
    assert IMPORT_TERMINAL_STATUSES == {ImportStatus.BASELINE, ImportStatus.DISCARDED}

    for target in ImportStatus:
        assert can_import_transition(ImportStatus.BASELINE, target) is False
        assert can_import_transition(ImportStatus.DISCARDED, target) is False
        assert can_import_transition(ImportStatus.BASELINE, ImportStatus.BASELINE) is False
        assert can_import_transition(ImportStatus.DISCARDED, ImportStatus.DISCARDED) is False


def test_importing_has_no_way_back_to_validating() -> None:
    """`IMPORTING` 的失败回退到 `FAILED`（写入异常/回滚），再经 `FAILED → DRAFT` 重来，
    不回 `VALIDATING`、不回 `VALIDATED`。方向必须钉死。"""
    assert can_import_transition(ImportStatus.IMPORTING, ImportStatus.FAILED) is True
    assert can_import_transition(ImportStatus.IMPORTING, ImportStatus.VALIDATING) is False
    assert can_import_transition(ImportStatus.IMPORTING, ImportStatus.VALIDATED) is False
    assert can_import_transition(ImportStatus.IMPORTING, ImportStatus.DRAFT) is False


# ------------------------------------------------------------------ 纯函数性质

def test_statuses_must_be_import_status_instances() -> None:
    """传字符串必须**报类型错**，不能静默判成「非法迁移」。

    理由与 `test_job_state.py::test_statuses_must_be_job_status_instances` 逐字相同：
    `ImportStatus` 是 `str` 枚举，`ImportStatus.DRAFT == "DRAFT"` 为真，但 `hash` 走
    成员名，于是裸字符串查表的结果取决于成员名与取值是否恰好同形。**类型错了就说类型错了。**
    """
    with pytest.raises(TypeError):
        can_import_transition("DRAFT", ImportStatus.VALIDATING)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        assert_import_transition(ImportStatus.DRAFT, "validating")  # type: ignore[arg-type]


def test_can_import_transition_is_deterministic() -> None:
    """同样输入必得同样输出（红线：核心链路本地确定性计算）。

    连同下面那条「不构造任何 IO 对象」一起，把 tasks 1.1 的「纯函数无 IO」变成可执行断言。
    """
    for pair in ALL_PAIRS:
        assert can_import_transition(*pair) == can_import_transition(*pair)


def test_assert_import_transition_does_not_raise_on_legal_pair_twice() -> None:
    """重复调用不累积状态（无副作用）—— 合法迁移调两次，两次都该通过。"""
    for pair in LEGAL_PAIRS:
        assert assert_import_transition(*pair) is pair[1]
        assert assert_import_transition(*pair) is pair[1]


#: 本模块允许出现的全部 import 来源。多一个都要在这里显式加 —— 加的时候会看见
#: 「我在往一个要求无 IO 的模块里塞依赖」。
_ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "types",  # MappingProxyType —— 让迁移表只读，不是 IO
    "typing",
    "app.core.enums",
    "app.core.errors",
}


def test_module_imports_no_io_dependencies() -> None:
    """tasks 1.1：状态机「纯函数无 IO」。

    用 AST 读自己的 import 清单，而不是「看代码觉得没 IO」—— 后者挡不住有人在
    状态机里 `from sqlalchemy.orm import Session` 去「顺手查一下库里那行」，
    那一刻状态机就不再是纯函数，而确定性会变成「取决于当时库里的状态」。
    """
    source = pathlib.Path(import_state.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= _ALLOWED_IMPORTS, f"状态机引入了不在白名单内的依赖：{imported - _ALLOWED_IMPORTS}"
