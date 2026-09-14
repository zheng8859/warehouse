"""③ 权重调优能力契约测试（tasks.md 3.4 的验证）。

事实来源：openspec/changes/ai-assist/design.md D12（>=50 批次门槛，反事实规则算，影子模式）
          spec `ai-assist`「③ 权重调优（≥50 批次，人采纳才生效）」

核心不变量：<50 批次 → `insufficient_samples` 不调 LLM；≥50 批次 → 反事实表**确定性**
（同输入同表），且建议落 `AiSuggestion(status=PROPOSED)` 影子模式（不写权重）。反事实表
的「提议权重」= 敏感性最高因子 +0.05、最低因子 −0.05（一加一减权重和不变）—— LLM 只把
它叙述成「建议表」，数字不改写（红线「规则算、LLM 只叙事」）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import JobStatus, JobType
from app.llm.capabilities import weight_tune
from app.models.configuration import WEIGHT_FACTORS
from app.models.job import JobOrder, PlanKind, RecommendationPlan
from app.models.llm import AiSuggestion, AiSuggestionStatus
from app.services.weight_tune import MIN_WEIGHT_TUNE_SAMPLES, build_counterfactual
from tests.logic.conftest import DEFAULT_WEIGHTS, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
PERIOD = "2026-09"
NOW = datetime(2026, 9, 14, 10, 0)

#: 反事实样本里「中性」的四个因子：候选巷道取值处处相等 → 敏感性恒 0，不参与提议权重
#: 的一加一减，让「谁被调高、谁被调低」在断言里是一目了然的常数。
_NEUTRAL = {"cap": 0.5, "existing": 0.5, "station": 0.5, "batch": 0.5}


def _sample(chosen: str = "01") -> dict[str, Any]:
    """一份合法的入库推荐理由（ReasonPayload 形状）：3 条候选巷 × 6 因子分解。

    `abc` 在选中巷取值 0.9、别处 0.1（敏感性最高 → 提议调高）；`continuity` 反过来
    （敏感性最低 → 提议调低）；其余四因子处处相等（敏感性 0）。
    """
    aisles = ["01", "02", "03"]
    breakdown: dict[str, dict[str, Any]] = {}
    for aisle in aisles:
        terms: dict[str, dict[str, Any]] = {
            "abc": {"value": 0.9 if aisle == chosen else 0.1, "note": f"abc@{aisle}"},
            "continuity": {
                "value": 0.1 if aisle == chosen else 0.9,
                "note": f"continuity@{aisle}",
            },
        }
        for factor, value in _NEUTRAL.items():
            terms[factor] = {"value": value, "note": f"{factor}@{aisle}"}
        breakdown[aisle] = terms
    return {"aisles": [chosen], "breakdown": breakdown, "factor_degraded": {}}


def _seed_samples(session: Session, n: int) -> None:
    """造 `n` 份入库作业单 + 分配方案（反事实模拟的历史批次样本）。"""
    for i in range(n):
        job = JobOrder(
            warehouse_id=WAREHOUSE,
            order_no=f"PO-{i:04d}",
            line_no="10",
            job_type=JobType.INBOUND,
            material_code="3001234",
            qty=10,
            batch_no=f"B{i:04d}",
            status=JobStatus.PENDING,
        )
        session.add(job)
        session.flush()
        session.add(
            RecommendationPlan(
                warehouse_id=WAREHOUSE,
                job_order_id=job.id,
                plan_kind=PlanKind.ASSIGN,
                payload_json=_sample(),
            )
        )
    session.flush()


# ------------------------------------------------------------------ 反事实表确定性（tasks 3.4 的验证）

def test_build_counterfactual_is_deterministic() -> None:
    """同输入同表（D12「反事实表确定性」）：同样样本与权重两次产出完全一致。"""
    samples = [_sample(), _sample("02"), _sample()]

    first = build_counterfactual(samples=samples, current_weights=DEFAULT_WEIGHTS)
    second = build_counterfactual(samples=samples, current_weights=DEFAULT_WEIGHTS)

    assert first == second


def test_build_counterfactual_bumps_top_and_cuts_bottom() -> None:
    """提议权重 = 敏感性最高因子 +0.05、最低 −0.05，其余不动（权重和不变）。"""
    result = build_counterfactual(
        samples=[_sample()], current_weights=DEFAULT_WEIGHTS
    )

    assert result["sample_size"] == 1
    assert result["current_weights"] == DEFAULT_WEIGHTS
    # abc 在选中巷取值最高（敏感性最高）→ +0.05；continuity 最低 → −0.05。
    assert result["proposed_weights"]["abc"] == pytest.approx(0.30)
    assert result["proposed_weights"]["continuity"] == pytest.approx(0.05)
    # 其余四因子不动。
    for factor in _NEUTRAL:
        assert result["proposed_weights"][factor] == DEFAULT_WEIGHTS[factor]
    # 权重和不变（0.30 + 0.20 + 0.15 + 0.20 + 0.10 + 0.05 = 1.00）。
    assert sum(result["proposed_weights"].values()) == pytest.approx(1.00)
    # 建议表逐因子给出：6 行，每行含当前/提议/敏感性。
    assert len(result["factors"]) == 6
    assert [row["factor"] for row in result["factors"]] == list(WEIGHT_FACTORS)
    assert result["sensitivity"]["abc"] == pytest.approx(0.5333, abs=1e-3)
    assert result["sensitivity"]["continuity"] == pytest.approx(-0.5333, abs=1e-3)


def test_build_counterfactual_expected_impact_reports_replay() -> None:
    """预期影响 = 选中巷道得分均值（当前 vs 提议）：调高「拉向选中巷」的因子应使均值上升。"""
    result = build_counterfactual(
        samples=[_sample(), _sample()], current_weights=DEFAULT_WEIGHTS
    )

    impact = result["expected_impact"]
    assert impact["chosen_score_mean_proposed"] > impact["chosen_score_mean_current"]


# ------------------------------------------------------------------ 能力编排（门槛 + 影子模式）

def test_weight_tune_insufficient_samples_degrades(session: Session) -> None:
    """<50 批次 → `insufficient_samples` 不调 LLM，也不落 AiSuggestion。"""
    make_scenario(session)  # 权重已配（load_weights 的前提），但样本不足。
    _seed_samples(session, n=3)

    result = weight_tune(
        session,
        warehouse_id=WAREHOUSE,
        settings=Settings(llm_provider="mock"),
        period=PERIOD,
        now=NOW,
    )

    assert result.ai_generated is False
    assert result.degraded_reason == "insufficient_samples"
    assert result.rule["metrics"]["sample_size"] == 3
    assert result.rule["metrics"]["required_sample_size"] == MIN_WEIGHT_TUNE_SAMPLES
    # 未达门槛不落建议（影子模式都没资格进）。
    assert session.query(AiSuggestion).count() == 0


def test_weight_tune_persists_shadow_suggestion(session: Session) -> None:
    """≥50 批次 → 反事实表 + AiSuggestion(PROPOSED) 影子模式，规则卡片带 suggestion_id。"""
    make_scenario(session)
    _seed_samples(session, n=MIN_WEIGHT_TUNE_SAMPLES)

    result = weight_tune(
        session,
        warehouse_id=WAREHOUSE,
        settings=Settings(llm_provider="mock"),
        period=PERIOD,
        now=NOW,
    )

    assert result.ai_generated is True
    # 规则卡片 = 反事实表的数字来源，提议权重是规则算的数。
    assert result.rule["metrics"]["sample_size"] == MIN_WEIGHT_TUNE_SAMPLES
    assert result.rule["metrics"]["proposed_weights"]["abc"] == pytest.approx(0.30)
    # 建议落影子模式（PROPOSED，不生效），context_json 存「拟采纳权重」供 weight/apply 读。
    suggestion_id = result.rule["suggestion_id"]
    suggestion = session.get(AiSuggestion, suggestion_id)
    assert suggestion is not None
    assert suggestion.status is AiSuggestionStatus.PROPOSED
    assert suggestion.context_json["proposed_weights"]["abc"] == pytest.approx(0.30)
    # LLM 叙事回填进建议文本（mock 回显非空）。
    assert suggestion.suggestion_text != ""
