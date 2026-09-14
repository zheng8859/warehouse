"""L1 巷道距离 / FIFO（4 道）：站台距离因子 + 既有库存（FIFO 抬分）+ 库位切片。

对应 20号 `SC-001`（近站台优先）、`SC-005`（既有同物料落位抬分）。
本层测 `station_factor` / `existing_factor` / `aisle_of` 三个纯函数的最小口径，
与 `tests/logic/test_factors.py` 不重复（后者覆盖降级与边界组合）。
"""
from __future__ import annotations

import pytest

from app.engine.factors import InventoryProfile, aisle_of, existing_factor, station_factor

pytestmark = pytest.mark.l1


def test_station_factor_near_beats_far():
    """SC-001：近站台距离权重（0.9）> 远巷道（0.3），且都不降级。"""
    near = station_factor(distance_weight=0.9)
    far = station_factor(distance_weight=0.3)
    assert not near.is_degraded and not far.is_degraded
    assert near.term.value > far.term.value


def test_station_factor_clamps_out_of_range():
    """站台距离权重夹取到 [0,1]（17 §2.2 未声明上界，读取侧夹取）。"""
    assert station_factor(distance_weight=1.5).term.value == 1.0
    assert station_factor(distance_weight=-0.2).term.value == 0.0
    # 缺失（None）⇒ 降级，理由可见（降级不静默）
    missing = station_factor(distance_weight=None)
    assert missing.is_degraded
    assert "AisleStation" in missing.degrade_reason


def test_existing_factor_fifo_more_plates_scores_higher():
    """SC-005：既有同物料板数越多得分越高（FIFO 收拢抬分）。"""
    order_cells = 10
    with_inv = existing_factor(
        profile=InventoryProfile(snapshot_present=True, plates_by_aisle={"01": 6}),
        aisle="01",
        order_cells=order_cells,
    )
    empty = existing_factor(
        profile=InventoryProfile(snapshot_present=True, plates_by_aisle={}),
        aisle="01",
        order_cells=order_cells,
    )
    assert with_inv.term.value == pytest.approx(0.6)   # min(1, 6/10)
    assert empty.term.value == 0.0                      # 无既有库存


def test_aisle_of_slices_first_two_chars():
    """库位号按文本切片 [:2] 得巷道，不得数值化（前导 0 不丢）。"""
    assert aisle_of("010104") == "01"
    assert aisle_of("021201") == "02"
    assert aisle_of("120101") == "12"
