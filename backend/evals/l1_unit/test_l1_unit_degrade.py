"""L1 降级链（5 道）：四级分档 + 逐级下探原因 + A 类告警 + 四级走尽处置。

对应 golden `golden_035`（cap 不足逐级降级）、`golden_040`（A 类降级告警 + 理由可见）。
场景可溯源 20号：`SC-*`（降级链顺序）、`AC-*`（降级不静默）。

本层只测 `resolve_tiers` / `tier_stop_reason` / `is_alarming` /
`near_station_alert_message` / `allocation_failure` 五个纯函数，分配器的逐档下探
编排由 `tests/logic/test_degradation.py` 覆盖。
"""
from __future__ import annotations

import pytest

from app.core.enums import AbcClass
from app.engine.degradation import (
    allocation_failure,
    is_alarming,
    near_station_alert_message,
    near_station_gap,
    resolve_tiers,
    tier_stop_reason,
)

pytestmark = pytest.mark.l1


def test_golden_035_four_tier_degrade_chain_order_and_membership():
    """golden_035：四级链顺序 = 近站台 → 次近巷道 → 远巷道 → 溢出区；NULL 归远巷道。"""
    plan = resolve_tiers(["01", "02", "03"], is_near_station={"01": True, "02": False, "03": None})
    assert [t.label for t in plan.tiers] == ["近站台", "次近巷道", "远巷道", "溢出区"]
    assert plan.tiers[0].aisles == ("01",)
    assert plan.tiers[1].aisles == ()            # 首期次近巷道无阈值 → 空档占位
    assert plan.tiers[2].aisles == ("02", "03")  # False 与 NULL 同归远巷道
    assert plan.unknown_near_station == ("03",)  # NULL 点名，不静默


def test_golden_035_cap_insufficient_tier_stop_reason():
    """golden_035：停在远巷道时写明「近站台无可行容量」+ 停档名；停档 0 无降级。"""
    plan = resolve_tiers(["01", "02"], is_near_station={"01": True, "02": False})
    reason = tier_stop_reason(plan=plan, stop_tier=plan.tiers[2])
    assert "近站台无可行容量" in reason
    assert "停在「远巷道」档" in reason
    # 停在档 0（近站台）⇒ 没有降级
    assert tier_stop_reason(plan=plan, stop_tier=plan.tiers[0]) is None


def test_golden_040_a_class_alarm_only_for_far_tier():
    """golden_040：A 类 ∩ 停在档 2/3 才告警；非 A 类、停档 0、未知档一律不告警。"""
    plan = resolve_tiers(["01", "02"], is_near_station={"01": True, "02": False})
    assert is_alarming(abc_class=AbcClass.A, stop_tier=plan.tiers[2]) is True
    assert is_alarming(abc_class=AbcClass.B, stop_tier=plan.tiers[2]) is False
    assert is_alarming(abc_class=AbcClass.A, stop_tier=plan.tiers[0]) is False
    assert is_alarming(abc_class=None, stop_tier=plan.tiers[2]) is False


def test_golden_040_alert_message_contains_gap_and_advice():
    """golden_040：告警文案逐字含「近站台缺口 X 板，建议移库腾挪」，缺口量可复现。"""
    plan = resolve_tiers(["01", "02"], is_near_station={"01": True, "02": False})
    assert near_station_gap(order_cells=10, near_station_remaining=4) == 6
    assert near_station_gap(order_cells=10, near_station_remaining=12) == 0  # 缺口不足为零
    message = near_station_alert_message(plan=plan, order_cells=10, near_station_remaining=4)
    assert "近站台缺口 6 板，建议移库腾挪" in message


def test_allocation_failure_human_intervention_no_aisle():
    """四级走尽 ⇒ 独立 `AllocationFailure`：无巷道字段、提示人工介入、停留 PENDING。"""
    plan = resolve_tiers(["01"], is_near_station={"01": False})
    failure = allocation_failure(job_order_id="JO-1", plan=plan)
    assert failure.job_order_id == "JO-1"
    assert "状态停留 PENDING" in failure.message
    assert "请人工介入" in failure.message
    assert "不出现在 plans" in failure.message
    assert not hasattr(failure, "aisle")
