"""L1 评分正确性（6 道）：`score_aisles` / `select_best` 纯函数，确定性 100%。

对应 golden `golden_001`（评分确定性）、`golden_006`（六因子满分=1.0）。
场景可溯源 20号：`SC-001`（近站台抬分）、`SC-002`（既有同物料抬分）、
`SC-004`（cap 满排除候选）、`SC-006`（六因子口径）。

本层只测「同样输入必得同样输出 + 满分恒为 1.0 + 因子值单调抬分 + 并列裁决」，
因子函数本身的正确性在 `tests/logic/test_scoring.py` 已覆盖，这里不复述。
"""
from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.engine.scoring import score_aisles, select_best
from app.models.configuration import WEIGHT_FACTORS
from app.schemas.reason import FactorTerm
from tests.logic.conftest import DEFAULT_WEIGHTS

pytestmark = pytest.mark.l1


def _term(value: float) -> FactorTerm:
    return FactorTerm(value=value, note=f"取值 {value}")


def _breakdown(aisles: list[str], values: Mapping[str, float]) -> dict[str, dict[str, FactorTerm]]:
    """给每条候选巷同一套六因子取值，构造 `score_aisles` 的 `breakdown`。"""
    return {a: {f: _term(values[f]) for f in WEIGHT_FACTORS} for a in aisles}


def test_golden_001_scores_are_deterministic():
    """golden_001：同输入两次 `score_aisles` 输出逐位一致（确定性 100%，20号 SC-006）。"""
    breakdown = _breakdown(
        ["01", "02", "03"],
        values={"abc": 1.0, "cap": 0.7, "existing": 0.6, "station": 0.9, "batch": 0.0, "continuity": 0.5},
    )
    first = score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown)
    second = score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown)
    assert first == second
    # 选道同样确定（并列全列、按 aisle_no 升序）
    assert select_best(first) == select_best(second)


def test_golden_006_full_six_factors_max_score_is_one():
    """golden_006：六因子全取 1.00 时每巷总分 = 1.00（满分恒为 1.0，17 §10.1）。"""
    breakdown = _breakdown(["01", "02"], values={f: 1.0 for f in WEIGHT_FACTORS})
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown)
    assert scores == {"01": 1.0, "02": 1.0}


def test_sc_001_near_station_factor_orders_aisles():
    """SC-001：站台距离权重越高得分越高（近站台抬分，其余因子相同）。"""
    near = {f: _term(0.5) for f in WEIGHT_FACTORS}
    near["station"] = _term(0.9)
    far = {f: _term(0.5) for f in WEIGHT_FACTORS}
    far["station"] = _term(0.3)
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown={"01": near, "02": far})
    assert scores["01"] > scores["02"]


def test_sc_002_existing_inventory_lifts_score():
    """SC-002：既有同物料落位抬升该巷得分（existing 值越高得分越高）。"""
    with_inv = {f: _term(0.5) for f in WEIGHT_FACTORS}
    with_inv["existing"] = _term(1.0)
    without = {f: _term(0.5) for f in WEIGHT_FACTORS}
    without["existing"] = _term(0.0)
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown={"01": with_inv, "02": without})
    assert scores["01"] > scores["02"]


def test_sc_004_select_best_returns_max_and_ties_ascending():
    """SC-004 / D16：选最高分巷道，并列同分全列且按 aisle_no 升序。"""
    high = {f: _term(0.9) for f in WEIGHT_FACTORS}
    low = {f: _term(0.1) for f in WEIGHT_FACTORS}
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown={"01": low, "02": high, "03": high})
    assert scores["02"] == scores["03"] > scores["01"]
    assert select_best(scores) == ["02", "03"]


def test_sc_006_degraded_factor_excluded_and_renormalized():
    """SC-006：降级因子不进分母，其余因子重新归一化后满分仍为 1.0。"""
    degraded = ("batch", "continuity")
    scored = [f for f in WEIGHT_FACTORS if f not in degraded]
    breakdown = {"01": {f: _term(1.0) for f in scored}}
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown, degraded=degraded)
    assert scores["01"] == 1.0
