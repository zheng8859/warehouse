"""L3 集中度趋势（5 道）：加权集中度 + 达成率判定 + Go/No-Go + 环比向好。

对应 golden `golden_046`（80% 拣货量落 ≤5 巷）、`golden_047`（threshold=0.8 参数化）、
`golden_045`（达成率判定 ≥70% 达标）、`golden_050`（达成率 ≥70% 判 Go / <70% 判 No-Go）、
`golden_048`（环比向好判定）。

L3 是「验收口径 + 冷路径观测」，非百分比门槛（design.md D2）：本层只把口径与判定逻辑测对，
真实达成率是试点期 runtime 活动。判定函数收在 `eval_utils`（`attainment_rate` /
`judge_attainment` / `trend`），与 run_evals 的 Go/No-Go 同源，不重复实现。
"""
from __future__ import annotations

import pytest

from evals.eval_utils import attainment_rate, judge_attainment, trend, weighted_concentration

pytestmark = pytest.mark.l3


def test_golden_046_weighted_concentration_80_percent_within_five_aisles():
    """golden_046：80% 拣货量落 ≤5 巷道（统一验收指标，18 §1.3 L36）。"""
    # 40 + 35 + 25 = 100；80% 阈值 = 80；前两条累加 75 < 80，第三条凑满 → N=3 ≤5。
    assert weighted_concentration([("01", 40), ("02", 35), ("03", 25)]) == 3
    # 一条巷全覆盖 → N=1。
    assert weighted_concentration([("01", 100)]) == 1
    # 空单 → 0。
    assert weighted_concentration([]) == 0


def test_golden_047_weighted_concentration_threshold_parameterized():
    """golden_047：`threshold=0.8` 时 N 计算正确（阈值参数化，不写死）。"""
    # 50/30/20：threshold=0.8 → 目标 80 → 前两条 80 达标 → N=2。
    assert weighted_concentration([("01", 50), ("02", 30), ("03", 20)], threshold=0.8) == 2
    # 同一分布 threshold=0.9 → 目标 90 → 前两条 80 不够，第三条凑满 → N=3。
    assert weighted_concentration([("01", 50), ("02", 30), ("03", 20)], threshold=0.9) == 3


def test_golden_045_attainment_rate_reaches_70_percent():
    """golden_045：达成率判定函数输出正确（≥70% 达标，18 §7.5）。"""
    # 10 张单，8 张 N≤5 → 达成率 0.8 ≥ 0.70 → 达标。
    concentrations = [3, 4, 5, 5, 2, 5, 6, 4, 8, 5]
    rate = attainment_rate(concentrations)
    assert rate == pytest.approx(0.8)
    assert judge_attainment(rate) is True


def test_golden_050_go_nogo_threshold():
    """golden_050：达成率 ≥70% 判 Go，<70% 判 No-Go（20号 §7.5 Go/No-Go 闸门）。"""
    assert judge_attainment(0.71) is True   # ≥0.70 → Go
    assert judge_attainment(0.70) is True   # 恰在阈值 → Go
    assert judge_attainment(0.69) is False  # <0.70 → No-Go
    # 达成率本身：7/10 = 0.70。
    assert attainment_rate([5, 5, 5, 5, 5, 5, 5, 6, 6, 6]) == pytest.approx(0.7)


def test_golden_048_trend_better_detection():
    """golden_048：环比向好判定正确（两周集中度对比，20号 §7.5 趋势向好）。"""
    # 达成率越高越好：0.72 → 0.78 向好。
    assert trend(0.78, 0.72, higher_is_better=True) == "better"
    # 跨巷道越低越好：3.0 → 2.0 向好。
    assert trend(2.0, 3.0, higher_is_better=False) == "better"
    # 相等 → 不劣化。
    assert trend(0.72, 0.72, higher_is_better=True) == "no_regression"
    # 恶化。
    assert trend(0.65, 0.72, higher_is_better=True) == "worse"
