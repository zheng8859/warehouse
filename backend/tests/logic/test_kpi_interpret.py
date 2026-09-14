"""① KPI 解读能力契约测试（tasks.md 3.2 的验证）。

事实来源：openspec/changes/ai-assist/design.md D1（最小聚合作为 ① rule 输入）
          spec `ai-assist`「① KPI 解读（只读）」

核心不变量：叙事中引用的数值与 `rule` 一致（LLM 不得改写数值）。mock 客户端把出站
提示词原样回显，故「出站提示词里的数值 == rule 卡片的数值」可被精确断言。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm.capabilities import build_kpi_rule, kpi_interpret, run_cold_path
from tests.logic.conftest import InventorySpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
PERIOD = "2026-09"


def test_build_kpi_rule_nests_metrics_under_the_allowlisted_key() -> None:
    """聚合指标挂在白名单键 `metrics` 下 —— 出境不被 `redact` 剥掉，规则卡片结构稳定。"""
    rule = build_kpi_rule(
        cross_aisle_mean=2.5,
        weighted_concentration=3,
        adoption_rate=0.68,
        material_code="3001234",
    )

    assert rule["material_code"] == "3001234"
    assert rule["metrics"]["same_material_cross_aisle_mean"] == 2.5
    assert rule["metrics"]["weighted_concentration"] == 3
    assert rule["metrics"]["adoption_rate"] == 0.68


def test_rule_numbers_match_ai_narrative_numbers(session: Session) -> None:
    """数值一致（spec ① 场景「KPI 解读数值一致」）：出站数值与 rule 逐项相等。"""
    settings = Settings(llm_provider="mock")
    rule = build_kpi_rule(cross_aisle_mean=2.5, weighted_concentration=3, adoption_rate=0.68)

    result = run_cold_path(
        settings=settings,
        session=session,
        warehouse_id=WAREHOUSE,
        period=PERIOD,
        rule=rule,
    )

    assert result.ai_generated is True
    # mock 回显 = "[mock] " + 出站 JSON；解析回来与 rule 的 metrics 逐项相等。
    sent = json.loads(result.ai.removeprefix("[mock] "))  # type: ignore[arg-type]
    assert sent["metrics"] == rule["metrics"]


def test_kpi_interpret_computes_cross_aisle_mean_from_snapshot(session: Session) -> None:
    """规则聚合：同物料跨巷道均值取自当前快照的库存分布（A 占 2 巷、B 占 1 巷 → 1.5）。"""
    settings = Settings(llm_provider="mock")
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010101", material_code="A", batch_no="B1", qty=10),
            InventorySpec(location_code="010201", material_code="A", batch_no="B1", qty=10),
            InventorySpec(location_code="020101", material_code="A", batch_no="B1", qty=10),
            InventorySpec(location_code="010103", material_code="B", batch_no="B2", qty=10),
        ],
    )

    result = kpi_interpret(
        session,
        warehouse_id=WAREHOUSE,
        settings=settings,
        snapshot_id=scenario.snapshot.id,  # type: ignore[union-attr]
    )

    assert result.rule["metrics"]["same_material_cross_aisle_mean"] == 1.5
    assert result.ai_generated is True


def test_kpi_interpret_degrades_without_a_snapshot(session: Session) -> None:
    """无快照 → 规则卡片仍产出（均值 0.0），LLM 侧降级不报错。"""
    result = kpi_interpret(
        session,
        warehouse_id=WAREHOUSE,
        settings=Settings(llm_provider="mock"),
    )

    assert result.rule["metrics"]["same_material_cross_aisle_mean"] == 0.0
    assert "metrics" in result.rule
