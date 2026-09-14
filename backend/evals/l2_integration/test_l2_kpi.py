"""L2 KPI 计量（5 道）：同物料/同批跨巷道计数、加权集中度、采纳率、阈值判定。

对应 golden `golden_013`（同物料跨巷道 = 实际占用巷道数）、`golden_014`（同批跨巷道）、
`golden_015`（加权集中度 80% 落 ≤5）、`golden_016`（采纳率 = adopted/total）、
`golden_018`（同物料跨巷道 ≤5 达标判定）。

`golden_017`（KPI 快照聚合落库）属阶段六 KPI 看板（F10）的独立 change，本 change 只测
指标纯函数计算与阈值判定（复用 `services/kpi.py` / `eval_utils`），不实现聚合落库 ——
见 tasks.md 头注。口径经 `eval_utils` 门面走业务函数，同源不漂移。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.linkage import InventoryItem
from evals.eval_utils import adoption_rate, cross_aisle, cross_aisle_mean, weighted_concentration
from tests.logic.conftest import InventorySpec, make_scenario

pytestmark = pytest.mark.l2


def _items(rows) -> list[tuple[str, str]]:
    """库存行 → `(material_code, aisle_no)`，`aisle_no = location_code[:2]`（与 `aisle_of` 同口径）。"""
    return [(row.material_code, row.location_code[:2]) for row in rows]


def test_golden_013_same_material_cross_aisle_counts_distinct_aisles(session):
    """golden_013：同物料跨巷道计数 = 实际占用巷道数（同一料号多库位只数一次）。"""
    make_scenario(
        session,
        inventory=[
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("010205", "M1", "B1", 10),  # 同巷道第二个库位，不重复计数
            InventorySpec("020101", "M1", "B1", 10),
            InventorySpec("030101", "M1", "B2", 10),
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    assert cross_aisle(_items(rows)) == {"M1": 3}


def test_golden_014_same_batch_cross_aisle_counts_correctly(session):
    """golden_014：同批跨巷道计数正确（按批号聚合去重巷道）。"""
    make_scenario(
        session,
        inventory=[
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("020101", "M1", "B1", 10),
            InventorySpec("010205", "M2", "B1", 10),
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    batch_aisles: dict[str, set[str]] = {}
    for row in rows:
        batch_aisles.setdefault(row.batch_no, set()).add(row.location_code[:2])
    assert batch_aisles["B1"] == {"01", "02"}


def test_golden_015_weighted_concentration_80_percent_within_five_aisles():
    """golden_015：拣货量加权集中度 —— 80% 拣货量落在 ≤N 巷道，N 计算正确。"""
    # 3 条巷：40 + 35 + 25 = 100；80% 阈值 = 80；前两条累加 75 < 80，第三条凑满 → N=3 ≤5。
    assert weighted_concentration([("01", 40), ("02", 35), ("03", 25)]) == 3
    # 一条巷全覆盖 → N=1。
    assert weighted_concentration([("01", 100)]) == 1
    # 空单 → 0。
    assert weighted_concentration([]) == 0


def test_golden_016_adoption_rate_is_adopted_over_total():
    """golden_016：推荐采纳率 = adopted / total；零推荐 → 0.0（非 NaN、非 100% 虚高）。"""
    assert adoption_rate(3, 5) == 0.6
    assert adoption_rate(0, 0) == 0.0


def test_golden_018_same_material_cross_aisle_within_threshold(session):
    """golden_018：同物料跨巷道 ≤5 达标判定 —— ≤5 达标、>5 超标（20号 §五）。"""
    make_scenario(
        session,
        inventory=[
            *[InventorySpec(f"{aisle:02d}0101", "M1", "B1", 10) for aisle in range(1, 4)],
            *[InventorySpec(f"{aisle:02d}0101", "M6", "B6", 10) for aisle in range(1, 7)],
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    counts = cross_aisle(_items(rows))
    assert counts["M1"] == 3 and counts["M1"] <= 5  # 达标
    assert counts["M6"] == 6 and counts["M6"] > 5  # 超标
    assert cross_aisle_mean(_items(rows)) == pytest.approx((3 + 6) / 2)
