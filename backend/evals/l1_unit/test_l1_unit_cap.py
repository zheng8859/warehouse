"""L1 cap 竞价/预留扣减（5 道）：可行巷道集三判据 + 预留池口径。

对应 golden `golden_033`（超总格 → 可行集空 → 四级走尽回滚，不静默落位）。
场景可溯源 20号：`SC-004`（cap 满排除）、`AC-001`（非 A 类不占预留池）、
`TR-*`（预留池释放钟点）。

本层只测 `feasible_aisles` / `available_cap` / `is_reserve_released` 三个纯函数的
判据边界，分配器的整单回滚（`allocate_batch`）由 `tests/logic` 覆盖。
"""
from __future__ import annotations

from datetime import datetime, time

import pytest

from app.core.enums import AbcClass
from app.engine.degradation import allocation_failure, resolve_tiers
from app.engine.reserved import available_cap, is_reserve_released
from app.engine.scoring import feasible_aisles, load_aisle_state
from tests.logic.conftest import AisleSpec, make_scenario

pytestmark = pytest.mark.l1

RELEASE_AT = time(18, 0)
NOW_BEFORE = datetime(2026, 9, 11, 9, 0)   # 释放钟点之前
NOW_AFTER = datetime(2026, 9, 11, 18, 30)  # 释放钟点之后


def _state(session, scenario):
    return load_aisle_state(
        session, warehouse_id=scenario.warehouse_id, snapshot_id=scenario.snapshot.id
    )


def test_golden_033_order_exceeds_all_caps_yields_empty_feasible(session):
    """golden_033：本单格数超过全部巷道可用容量 → 可行集空（四级走尽，不静默落位）。"""
    sc = make_scenario(
        session, aisles=[AisleSpec("01", cap_total=5), AisleSpec("02", cap_total=3)]
    )
    state = _state(session, sc)
    feasible = feasible_aisles(
        state, abc_class=AbcClass.A, order_cells=10, release_at=RELEASE_AT, now=NOW_BEFORE
    )
    assert feasible == []
    # 四级走尽的处置：无巷道字段、提示人工介入（AC-005 由「没有巷道可落」结构上兑现）
    failure = allocation_failure(job_order_id="JO-1", plan=resolve_tiers([], is_near_station={}))
    assert not hasattr(failure, "aisle")
    assert "状态停留 PENDING" in failure.message
    assert "请人工介入" in failure.message


def test_sc_004_cap_full_aisle_excluded_from_feasible(session):
    """SC-004：已占满（可用 − 已占 < 本单格数）的巷道不进候选集。"""
    sc = make_scenario(
        session, aisles=[AisleSpec("01", cap_total=5), AisleSpec("02", cap_total=10)]
    )
    state = _state(session, sc)
    feasible = feasible_aisles(
        state,
        abc_class=AbcClass.A,
        order_cells=5,
        release_at=RELEASE_AT,
        now=NOW_BEFORE,
        consumed={"01": 5},  # 01 已占满，剩余 0
    )
    assert feasible == ["02"]


def test_ac_001_non_a_class_cannot_use_reserved_pool(session):
    """AC-001：非 A 类在释放钟点前只能拿 `cap_usable`（预留池不开放），A 类拿全额。"""
    sc = make_scenario(session, aisles=[AisleSpec("01", cap_total=100, cap_reserved=40)])
    cap = sc.aisle_caps["01"]
    assert cap.cap_usable == 60
    assert available_cap(cap=cap, abc_class=AbcClass.B, release_at=RELEASE_AT, now=NOW_BEFORE) == 60
    assert available_cap(cap=cap, abc_class=AbcClass.A, release_at=RELEASE_AT, now=NOW_BEFORE) == 100
    # abc_class 为 None（分类未派生）按非 A 类保守处理
    assert available_cap(cap=cap, abc_class=None, release_at=RELEASE_AT, now=NOW_BEFORE) == 60


def test_reserve_released_after_release_time(session):
    """TR：预留池在释放钟点（含边界）后对非 A 类开放为全额。"""
    sc = make_scenario(session, aisles=[AisleSpec("01", cap_total=100, cap_reserved=40)])
    cap = sc.aisle_caps["01"]
    assert is_reserve_released(release_at=RELEASE_AT, now=NOW_BEFORE) is False
    assert is_reserve_released(release_at=RELEASE_AT, now=NOW_AFTER) is True
    assert available_cap(cap=cap, abc_class=AbcClass.B, release_at=RELEASE_AT, now=NOW_AFTER) == 100


def test_batch_consumption_subtracts_from_available(session):
    """本批已占格数（`consumed`）从可用容量扣减后再判「cap 足够」。"""
    sc = make_scenario(session, aisles=[AisleSpec("01", cap_total=10)])
    state = _state(session, sc)
    consumed = {"01": 8}  # 剩余 2
    assert feasible_aisles(
        state, abc_class=AbcClass.A, order_cells=2, release_at=RELEASE_AT, now=NOW_BEFORE, consumed=consumed
    ) == ["01"]
    assert feasible_aisles(
        state, abc_class=AbcClass.A, order_cells=3, release_at=RELEASE_AT, now=NOW_BEFORE, consumed=consumed
    ) == []
