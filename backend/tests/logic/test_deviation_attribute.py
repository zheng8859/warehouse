"""② 偏离归因能力契约测试（tasks.md 3.3 的验证）。

事实来源：openspec/changes/ai-assist/design.md D11（异常清单规则算，LLM 只叙事）
          spec `ai-assist`「② 偏离归因（只读，不断言唯一根因）」

核心不变量：规则侧产出**确定性**异常清单（容量 / 降级 / 人工 / 非系统四类候选，各附
事实依据），LLM 只把清单串成话、不断言唯一根因 —— 同输入必得同清单（「规则算、LLM
只叙事」的 ② 落点）。四类候选的「事实」来自规则侧数据（方案级降级 = 容量、因子级
降级 = 降级、实际落位 vs 推荐巷道 = 人工、偏离成因 = 非系统），LLM 不得新增事实。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm.capabilities import build_deviation_rule, deviation_attribute
from app.models.job import (
    Deviation,
    DeviationCauseKind,
    PlanKind,
    RecommendationPlan,
)
from tests.logic.conftest import JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
PERIOD = "2026-09"


def _seed_deviation_scenario(session: Session, *, actual_location_code: str | None = None):
    """造一个「已偏离」的入库场景：作业单 + 推荐方案 + 偏离行。返回 (job, payload)。"""
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="PO-2026-007",
                material_code="3001234",
                qty=10,
                batch_no="B20260901",
            )
        ],
    )
    job = scenario.job_orders[0]
    if actual_location_code is not None:
        job.actual_location_code = actual_location_code
    payload = {
        "job_id": str(job.id),
        "aisles": ["01", "02"],
        "factors": {
            "abc": 0.25,
            "cap": 0.20,
            "existing": 0.15,
            "station": 0.20,
            "batch": 0.10,
            "continuity": 0.10,
        },
        "scores": {"01": 0.86, "02": 0.81},
        "breakdown": {
            "01": {"cap": {"value": 0.72, "note": "可用 58 / 80 板"}},
            "02": {"cap": {"value": 0.60, "note": "可用 48 / 80 板"}},
        },
        "priority": {"score": 0.5, "terms": {"outbound_qty": 0.0, "abc": 1.0, "existing_gain": 0.0}},
        "factor_degraded": {"station": "巷道-站台主数据未导出"},
        "degraded": False,
        "degrade_reason": None,
        "predicted_cross_aisle": {"material": 2, "threshold": 5, "exceeded": False},
    }
    session.add(
        RecommendationPlan(
            warehouse_id=WAREHOUSE,
            job_order_id=job.id,
            plan_kind=PlanKind.ASSIGN,
            payload_json=payload,
        )
    )
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            batch_no="B20260901",
            material_code="3001234",
            actual_cross_aisle=4,
            threshold_cross_aisle=3,
            cause_kind=DeviationCauseKind.NEW_INBOUND_SHORTFALL,
        )
    )
    session.flush()
    return job, payload


# ------------------------------------------------------------------ 清单确定性（tasks 3.3 的验证）

def test_build_deviation_rule_is_deterministic() -> None:
    """同输入同清单（D11「清单确定性」）：同样事实两次产出完全一致的规则卡片。"""
    kwargs = dict(
        material_code="3001234",
        batch_no="B1",
        actual_cross_aisle=4,
        threshold_cross_aisle=3,
        recommended_aisles=["02", "01"],
        factor_scores={"cap": 0.2, "station": 0.2},
        factor_degraded={"station": "巷道-站台主数据未导出"},
        plan_degraded=False,
        plan_degrade_reason=None,
        cap_note="可用 58 / 80 板",
        actual_location_code="030101",
        cause_kind=DeviationCauseKind.NEW_INBOUND_SHORTFALL.value,
    )

    first = build_deviation_rule(**kwargs)
    second = build_deviation_rule(**kwargs)

    assert first == second


def test_build_deviation_rule_lists_four_candidates_with_facts() -> None:
    """四类候选（容量/降级/人工/非系统）恒在、顺序固定，各自附规则侧事实依据。"""
    rule = build_deviation_rule(
        material_code="3001234",
        batch_no="B1",
        actual_cross_aisle=4,
        threshold_cross_aisle=3,
        recommended_aisles=["01", "02"],
        factor_scores={"cap": 0.2},
        factor_degraded={"station": "巷道-站台主数据未导出"},
        plan_degraded=True,
        plan_degrade_reason="主巷道容量不足，走降级链",
        cap_note="可用 58 / 80 板",
        actual_location_code="030101",
        cause_kind=DeviationCauseKind.NEW_INBOUND_SHORTFALL.value,
    )

    candidates = rule["metrics"]["candidates"]
    assert [c["category"] for c in candidates] == ["容量", "降级", "人工", "非系统"]

    capacity, degraded, manual, nonsystem = candidates
    # 容量：方案级降级（容量不足）+ 分配时点的 cap 事实。
    assert any("主巷道容量不足，走降级链" in e for e in capacity["evidence"])
    assert any("可用 58 / 80 板" in e for e in capacity["evidence"])
    # 降级：因子级降级逐条列原因。
    assert any("station" in e for e in degraded["evidence"])
    # 人工：实际落位（03 巷）不在推荐巷道（01/02）内。
    assert any("030101" in e for e in manual["evidence"])
    # 非系统：成因分类逐条给出。
    assert any("新入库收拢不达标" in e for e in nonsystem["evidence"])


def test_build_deviation_rule_no_facts_yields_empty_evidence() -> None:
    """无事实时各候选证据为空列表，不是「捏造一句」—— LLM 只能叙事已有事实。"""
    rule = build_deviation_rule(material_code="3001234")

    assert all(c["evidence"] == [] for c in rule["metrics"]["candidates"])
    assert rule["metrics"]["recommended_aisles"] == []
    assert rule["metrics"]["factor_scores"] == {}


# ------------------------------------------------------------------ 能力编排（拉多源明细 → 规则卡片）

def test_deviation_attribute_pulls_multi_source_detail(session: Session) -> None:
    """规则侧拉「推荐巷道集 + 6 因子分值 + 实际落位 + cap 事实」进规则卡片。"""
    _seed_deviation_scenario(session, actual_location_code="030101")

    result = deviation_attribute(
        session,
        warehouse_id=WAREHOUSE,
        material_code="3001234",
        batch_no="B20260901",
        settings=Settings(llm_provider="mock"),
        period=PERIOD,
    )

    rule = result.rule
    assert rule["material_code"] == "3001234"
    assert rule["metrics"]["recommended_aisles"] == ["01", "02"]
    assert rule["metrics"]["factor_scores"]["cap"] == 0.20
    assert rule["metrics"]["actual_location_code"] == "030101"
    assert rule["metrics"]["actual_cross_aisle"] == 4
    # 偏离成因落进「非系统」候选的证据里。
    nonsystem = rule["metrics"]["candidates"][3]
    assert any("新入库收拢不达标" in e for e in nonsystem["evidence"])
    assert result.ai_generated is True


def test_deviation_attribute_degrades_without_any_records(session: Session) -> None:
    """无偏离、无推荐方案时规则卡片仍产出（空证据），LLM 侧降级不报错。"""
    result = deviation_attribute(
        session,
        warehouse_id=WAREHOUSE,
        material_code="不存在",
        settings=Settings(llm_provider="mock"),
        period=PERIOD,
    )

    assert result.rule["material_code"] == "不存在"
    assert result.rule["metrics"]["recommended_aisles"] == []
    assert all(
        c["evidence"] == [] for c in result.rule["metrics"]["candidates"]
    )
