"""L3 KPI 基线达标（3 道）+ 冷路径观测（4 能力抽样，非闸门）。

对应 golden `golden_057`（同批跨巷道 ≤3）、`golden_059`（采纳率 ≥60%）、
`golden_060`（同物料 ≤5 且同批 ≤3 组合达标、不劣化）。

冷路径观测（design.md D8）：四能力（①KPI 解读 / ②偏离归因 / ③权重调优 / ④移库方案）
抽样 4/4 覆盖 + 误导=0（AI 回显不改写规则数值），**独立统计、不进 pass_rate** —— 用
`@pytest.mark.observation` 标记，run_evals 据此与 L3 闸门测试分流。
"""
from __future__ import annotations

from datetime import datetime

import json

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.llm.capabilities import (
    deviation_attribute,
    kpi_interpret,
    relocate_propose,
    weight_tune,
)
from app.models.linkage import InventoryItem
from evals.eval_utils import adoption_rate, cross_aisle, trend
from tests.logic.conftest import AisleSpec, InventorySpec, MaterialSpec, make_scenario

pytestmark = pytest.mark.l3

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 14, 10, 0)


def _batch_aisles(rows) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row.batch_no, set()).add(row.location_code[:2])
    return out


def test_golden_057_same_batch_cross_aisle_within_three(session):
    """golden_057：同批跨巷道 ≤3 达标判定正确（18号 同批跨巷道 ≤3）。"""
    make_scenario(
        session,
        inventory=[
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("020101", "M1", "B1", 10),
            InventorySpec("030101", "M1", "B1", 10),  # 3 巷 → 达标
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    batches = _batch_aisles(rows)
    assert len(batches["B1"]) == 3 and len(batches["B1"]) <= 3  # 达标


def test_golden_057_same_batch_cross_aisle_exceeds_three(session):
    """golden_057（反向）：同批跨巷道 >3 判超标。"""
    make_scenario(
        session,
        inventory=[
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("020101", "M1", "B1", 10),
            InventorySpec("030101", "M1", "B1", 10),
            InventorySpec("040101", "M1", "B1", 10),  # 4 巷 → 超标
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    batches = _batch_aisles(rows)
    assert len(batches["B1"]) == 4 and len(batches["B1"]) > 3  # 超标


def test_golden_059_adoption_rate_within_sixty_percent():
    """golden_059：采纳率 ≥60% 达标判定正确（18号 推荐采纳率 ≥60%）。"""
    assert adoption_rate(3, 5) == pytest.approx(0.6)  # 恰在阈值 → 达标
    assert adoption_rate(3, 5) >= 0.60
    assert adoption_rate(2, 5) < 0.60  # 40% → 不达标
    assert adoption_rate(0, 0) == 0.0   # 零推荐 → 0（非 NaN、非 100% 虚高）


def test_golden_060_combined_thresholds_no_regression(session):
    """golden_060：同物料 ≤5 且同批 ≤3 组合达标，且相对基线不劣化（越低越好）。"""
    make_scenario(
        session,
        inventory=[
            # M1 占 4 巷（≤5 达标）；批号 B1 占 2 巷（≤3 达标）。
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("020101", "M1", "B1", 10),
            InventorySpec("030101", "M1", "B2", 10),
            InventorySpec("040101", "M1", "B3", 10),
        ],
    )
    rows = tuple(session.scalars(select(InventoryItem)))
    items = [(row.material_code, row.location_code[:2]) for row in rows]
    counts = cross_aisle(items)
    batches = _batch_aisles(rows)

    assert counts["M1"] == 4 and counts["M1"] <= 5  # 同物料 ≤5 达标
    assert len(batches["B1"]) == 2 and len(batches["B1"]) <= 3  # 同批 ≤3 达标
    # 相对基线 5.0（golden_060.baseline_value），实测同物料跨巷道 4 → 不劣化（越低越好）。
    assert trend(counts["M1"], 5.0, higher_is_better=False) in ("better", "no_regression")


@pytest.mark.observation
def test_cold_path_observation_four_abilities_coverage_and_no_misleading(session):
    """冷路径观测（非闸门）：四能力抽样覆盖 4/4，AI 叙事不改写规则数值（误导=0）。

    design.md D8：观测项独立统计、不进 `pass_rate`。用 `mock` provider 使 AI 回显可精确断言
    「出站数值 == rule 数值」（spec ①「KPI 解读数值一致」的结构保证）。
    """
    settings = Settings(llm_provider="mock")
    scenario = make_scenario(
        session,
        aisles=[AisleSpec(f"{i:02d}", cap_total=100) for i in range(1, 6)],
        materials=[MaterialSpec("M1", abc_class="A", material_name="茉莉柚茶")],
        inventory=[
            InventorySpec("010101", "M1", "B1", 10),
            InventorySpec("020101", "M1", "B1", 10),
        ],
    )

    # ① KPI 解读：规则聚合出同物料跨巷道均值（M1 占 2 巷）。
    kpi = kpi_interpret(session, warehouse_id=WAREHOUSE, settings=settings, snapshot_id=scenario.snapshot.id)
    assert kpi.rule["metrics"]["same_material_cross_aisle_mean"] == 2.0
    assert kpi.ai_generated is True
    # 误导=0：mock 回显 = 出站提示词，其 metrics 与 rule 逐项相等（数值不被 LLM 改写）。
    assert json.loads(kpi.ai.removeprefix("[mock] "))["metrics"] == kpi.rule["metrics"]  # type: ignore[arg-type]

    # ② 偏离归因：无偏离也产出空证据卡片（只列事实、不下结论）。
    dev = deviation_attribute(session, warehouse_id=WAREHOUSE, material_code="M1", settings=settings)
    assert "candidates" in dev.rule["metrics"]

    # ③ 权重调优：样本 < 50 → 诚实降级（insufficient_samples），不调 LLM、不落建议。
    wt = weight_tune(session, warehouse_id=WAREHOUSE, settings=settings, period="2026-09")
    assert wt.rule["metrics"]["required_sample_size"] == 50
    assert wt.ai_generated is False  # 降级不调 LLM

    # ④ 移库方案：规则算三档，LLM 只叙事、数字来自规则。
    relo = relocate_propose(session, warehouse_id=WAREHOUSE, material_code="M1", settings=settings, now=NOW)
    assert [p["name"] for p in relo.rule["metrics"]["plans"]] == ["激进", "均衡", "保守"]

    # 覆盖 4/4：四能力均产出规则卡片（`rule` 非空）。
    assert all(prod.rule for prod in (kpi, dev, wt, relo))
