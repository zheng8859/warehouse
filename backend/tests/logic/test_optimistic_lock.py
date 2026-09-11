"""乐观锁校验辅助的逻辑测试。

事实来源：openspec/changes/data-model-permission/specs/data-model/spec.md
            「乐观锁并发守卫」的两个 Scenario
          openspec/changes/data-model-permission/design.md D1（版本语义三分）
          验证：tasks.md 1.6

**不建表、不连库**：被守护的是「比较 + 报错」这一段纯逻辑，与存储无关。
放进 `tests/logic/` 而非 `tests/models/`，正是因为它不该需要 session。
用轻量替身而非真模型，也让这些用例在 `JobOrder` / `ImportSession` 落地之前
就能钉住契约（两者分别属任务 4.1 与 3.1）。

真模型的接入测试（`JobOrder` 上真的并发确认）属阶段四 —— 那需要**文件库**，
内存库测不出跨连接的事务行为，见 `tests/conftest.py` 的模块 docstring。
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.core.concurrency import (
    LOCK_VERSION_ATTR,
    assert_lock_version,
    bump_lock_version,
)
from app.core.errors import DomainError, StateConflict

pytestmark = pytest.mark.logic


@dataclass
class _Locked:
    """带乐观锁列的替身（对应 D1 表格里的 JobOrder / ImportSession）。"""

    lock_version: int = 3


@dataclass
class _Versioned:
    """只带业务版本列的替身（对应 WeightConfig / CapacityConfig）。"""

    version_no: int = 3


@dataclass
class _Plain:
    """两者都没有的替身 —— 不在 D1 的乐观锁名单内。"""

    order_no: str = "JO-1"


# --------------------------------------------------------------- 匹配通过

def test_matching_version_passes() -> None:
    """版本一致即放行。"""
    assert assert_lock_version(_Locked(lock_version=3), expected=3) is None


def test_zero_version_passes() -> None:
    """0 是合法初值 —— 新单未被人动过就是 0，不能被当成「缺失」拦下。"""
    assert_lock_version(_Locked(lock_version=0), expected=0)


# --------------------------------------------------------------- 不匹配拒绝

def test_stale_version_is_rejected() -> None:
    """spec Scenario「并发确认仅一方成功」的后到者视角。

    两端都读到 3，先到者推进到 4；后到者仍拿着 3 提交，必须被拒绝 ——
    这正是「不得静默覆盖先到的写入」。
    """
    order = _Locked(lock_version=4)  # 先到者已提交，版本推进到 4
    with pytest.raises(StateConflict) as excinfo:
        assert_lock_version(order, expected=3)

    assert isinstance(excinfo.value, DomainError), "StateConflict 必须是 DomainError 子类"
    assert excinfo.value.http_status == 409


def test_rejection_carries_expected_and_actual() -> None:
    """报错要带上两侧的版本值。

    没有这两项，API 层只能说「冲突了」，前端无从判断是「别人改过」还是自己的
    bug；有了它们，「请重新读取」这个动作才有依据。
    """
    with pytest.raises(StateConflict) as excinfo:
        assert_lock_version(_Locked(lock_version=7), expected=3)

    detail = excinfo.value.detail
    assert detail["expected"] == 3
    assert detail["actual"] == 7
    assert detail["entity"] == "_Locked"


def test_version_does_not_move_on_rejection() -> None:
    """被拒时不得改动实例 —— 拒绝必须是纯粹的「不写」。"""
    order = _Locked(lock_version=9)
    with pytest.raises(StateConflict):
        assert_lock_version(order, expected=3)
    assert order.lock_version == 9


# --------------------------------------------------------------- 推进

def test_bump_advances_by_one() -> None:
    """spec Scenario 的「先提交者成功并推进版本」。"""
    order = _Locked(lock_version=3)
    assert bump_lock_version(order, expected=3) == 4
    assert order.lock_version == 4


def test_bump_leaves_instance_untouched_on_mismatch() -> None:
    """推进失败时不得留下半成品状态（校验过了但没加一 / 加了但没报错）。"""
    order = _Locked(lock_version=3)
    with pytest.raises(StateConflict):
        bump_lock_version(order, expected=2)
    assert order.lock_version == 3


def test_second_confirmation_with_stale_version_is_rejected() -> None:
    """端到端的两端竞争：先到成功、后到失败，且版本只推进了一次。

    这条把前两个用例连起来 —— 分开测时各自都对，仍可能因为「校验与推进
    用了不同的读数」而在真实序列里漏掉冲突。
    """
    order = _Locked(lock_version=3)

    both_saw = order.lock_version  # 两端同时读到 3

    bump_lock_version(order, expected=both_saw)  # 先到者

    with pytest.raises(StateConflict):
        bump_lock_version(order, expected=both_saw)  # 后到者，仍拿着 3

    assert order.lock_version == 4, "版本只应推进一次"


# --------------------------------------------------------------- 调用方错误

def test_business_version_column_is_not_accepted_as_lock_column() -> None:
    """D1：`version_no` 不能当乐观锁用，传错了要指名报出来。

    这是 D1「版本语义三分」最容易被违反的方式 —— 两个列都是整数、都能比较，
    代码跑得通，直到有人把配置业务版本当成并发版本读，红线当场失效。
    """
    with pytest.raises(TypeError) as excinfo:
        assert_lock_version(_Versioned(version_no=3), expected=3)
    assert "version_no" in str(excinfo.value)


def test_entity_without_any_version_column_is_reported() -> None:
    """既无 lock_version 也无业务版本的实体，报错要说明「本就不需要守卫」。"""
    with pytest.raises(TypeError) as excinfo:
        assert_lock_version(_Plain(), expected=0)
    assert LOCK_VERSION_ATTR in str(excinfo.value)


@pytest.mark.parametrize("bad", ["3", 3.0, None, True])
def test_non_integer_expected_is_rejected(bad: object) -> None:
    """HTTP 来的版本号天生是字符串，必须显式挡掉。

    放任比较的话 `"3" != 3` 成立，于是调用方的类型错误会以「版本冲突」的面目
    报出来 —— 排查方向被彻底带偏。`True` 也在此列：`True == 1`，一个误传的
    开关会与 `lock_version=1` 撞上，在最该拦住的格子上放行。
    """
    with pytest.raises(TypeError):
        assert_lock_version(_Locked(lock_version=3), expected=bad)  # type: ignore[arg-type]


def test_negative_expected_is_rejected() -> None:
    """负版本号是调用方算错了，不该以「冲突」的形式掩盖。"""
    with pytest.raises(ValueError):
        assert_lock_version(_Locked(lock_version=0), expected=-1)
