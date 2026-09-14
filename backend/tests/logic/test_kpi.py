"""KPI 最小聚合（阶段六收编点）口径测试（tasks.md 3.1 的验证）。

事实来源：18-KPI 与验收度量设计 §1.3（同物料跨巷道均值 / 拣货量加权集中度 / 采纳率）
          openspec/changes/ai-assist/design.md D1（最小聚合，作为 ①② 的 rule 输入）

口径钉死（与 18 号逐条对齐）：
- 同物料跨巷道均值（§1.3 L39）：全库所有物料的跨巷道数取均值（目标 ≤5）。
- 拣货量加权集中度（§1.3 L36）：单 DO 按拣货量降序累加至 80% 所覆盖的巷道数 N（N≤5）。
- 推荐采纳率（§1.3 L41）：接受/微调后确认 ÷ 总推荐（目标 ≥60%）。
"""
from __future__ import annotations

import pytest

from app.services.kpi import (
    adoption_rate,
    same_material_cross_aisle_mean,
    weighted_concentration,
)

pytestmark = pytest.mark.logic


# ------------------------------------------------------------------ 同物料跨巷道均值

def test_cross_aisle_mean_is_mean_over_materials_not_rows() -> None:
    """分母是「物料数」不是「库存行数」：一料多行只数一次巷道。"""
    items = [
        ("A", "01"), ("A", "01"), ("A", "02"), ("A", "03"),   # A 占 3 巷（重复行不重复数）
        ("B", "01"), ("B", "01"),                               # B 占 1 巷
    ]
    # A=3, B=1 → 均值 2.0；若按行数会错成 (3+1)/7。
    assert same_material_cross_aisle_mean(items) == 2.0


def test_cross_aisle_mean_on_empty_is_zero_not_error() -> None:
    """无库存 → 0.0，不抛异常（STALE 由调用方标注，这里不硬卡）。"""
    assert same_material_cross_aisle_mean([]) == 0.0


def test_cross_aisle_mean_single_material() -> None:
    """单物料退化：均值 = 该物料的巷道数。"""
    assert same_material_cross_aisle_mean([("A", "01"), ("A", "05"), ("A", "05")]) == 2.0


# ------------------------------------------------------------------ 拣货量加权集中度

def test_weighted_concentration_counts_aisles_to_80_percent() -> None:
    """18 §1.3：降序累加至 80% 覆盖的巷道数。50+30=80 ≥ 80% → N=2。"""
    aisle_qty = [("01", 50), ("02", 30), ("03", 15), ("04", 5)]  # 总量 100，80% = 80
    assert weighted_concentration(aisle_qty) == 2


def test_weighted_concentration_needs_three_aisles_when_qty_is_flat() -> None:
    """40+20+20=80 → N=3（数量较平时需要更多巷道才凑到 80%）。"""
    assert weighted_concentration([("01", 40), ("02", 20), ("03", 20), ("04", 20)]) == 3


def test_weighted_concentration_is_order_independent() -> None:
    """入参顺序无关（内部按数量降序）。"""
    a = weighted_concentration([("03", 15), ("01", 50), ("04", 5), ("02", 30)])
    b = weighted_concentration([("01", 50), ("02", 30), ("03", 15), ("04", 5)])
    assert a == b == 2


def test_weighted_concentration_on_empty_or_zero_is_zero() -> None:
    """空单 / 零量 → 0，不抛异常。"""
    assert weighted_concentration([]) == 0
    assert weighted_concentration([("01", 0)]) == 0


def test_weighted_concentration_threshold_is_configurable() -> None:
    """N 默认 5、阈值默认 80%，可配（18 §1.3「默认 N=5，可配」）。"""
    aisle_qty = [("01", 60), ("02", 20), ("03", 20)]  # 总量 100，90% = 90
    # 60+20=80 < 90，60+20+20=100 ≥ 90 → N=3
    assert weighted_concentration(aisle_qty, threshold=0.9) == 3


# ------------------------------------------------------------------ 推荐采纳率

def test_adoption_rate_is_adopted_over_total() -> None:
    """34/50 = 0.68（18 §七示例「采纳率 68%」）。"""
    assert adoption_rate(34, 50) == pytest.approx(0.68)


def test_adoption_rate_guards_divide_by_zero() -> None:
    """零推荐 → 0.0（不是 NaN / 异常）。"""
    assert adoption_rate(0, 0) == 0.0


def test_adoption_rate_full_and_empty() -> None:
    assert adoption_rate(10, 10) == 1.0
    assert adoption_rate(0, 10) == 0.0
