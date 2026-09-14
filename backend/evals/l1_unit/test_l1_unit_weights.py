"""L1 权重归一化（4 道）：六项权重和 = 1、权重=0 贡献=0、降级重归一化、加权平均。

对应 30号 §6.5「权重=0 时贡献=0」与 17 §10.1 的可追溯恒等式（分母按参与评分的
因子重新归一化，满分恒为 1.0）。`normalize_weights` 门面同源已在
`test_eval_utils.py` 验过，这里直接走 `score_aisles` 的**真实加权路径**。
"""
from __future__ import annotations

import pytest

from app.engine.scoring import score_aisles
from app.models.configuration import WEIGHT_FACTORS
from app.schemas.reason import FactorTerm
from tests.logic.conftest import DEFAULT_WEIGHTS

pytestmark = pytest.mark.l1


def _term(value: float) -> FactorTerm:
    return FactorTerm(value=value, note=f"取值 {value}")


def test_default_weights_sum_to_one():
    """17 §10.1 的示例权重和为 1.00（满分恒为 1.0 的分母前提）。"""
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    assert set(DEFAULT_WEIGHTS) == set(WEIGHT_FACTORS)


def test_weight_zero_contributes_zero():
    """30号 §6.5：某因子权重 = 0 时，该因子取值不再影响总分。"""
    weights = dict(DEFAULT_WEIGHTS)
    weights["abc"] = 0.0
    high = {f: _term(0.5) for f in WEIGHT_FACTORS}
    high["abc"] = _term(1.0)
    low = {f: _term(0.5) for f in WEIGHT_FACTORS}
    low["abc"] = _term(0.0)
    scores = score_aisles(weights=weights, breakdown={"01": high, "02": low})
    assert scores["01"] == scores["02"]


def test_weighted_average_all_half_is_half():
    """所有因子取值 0.5 ⇒ 加权平均 = 0.5（权重和为 1 时与权重无关）。"""
    breakdown = {"01": {f: _term(0.5) for f in WEIGHT_FACTORS}}
    assert score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown)["01"] == 0.5


def test_renormalized_denominator_keeps_full_score_one():
    """降级因子不进分母，其余因子重新归一化后满分仍为 1.0（17 §10.1 恒等式）。"""
    degraded = ("batch", "continuity")
    scored = [f for f in WEIGHT_FACTORS if f not in degraded]
    breakdown = {"01": {f: _term(1.0) for f in scored}}
    scores = score_aisles(weights=DEFAULT_WEIGHTS, breakdown=breakdown, degraded=degraded)
    assert scores["01"] == 1.0
