"""L3 移库有效性（4 道）：三档方案 + 三重校验 + 移库执行台账不改批号 + 移库后跨巷道下降。

对应 golden `golden_049`（三档方案 + 量化代价）、`golden_051`（三重校验通过才采纳）、
`golden_052`（台账更新巷道、批号不变）、`golden_055`（移库后跨巷道下降，趋势向好）。

移库多方案与三重校验是**规则算**（`services/relocate.py`，确定性纯函数），LLM 只叙事
（冷路径红线）。golden_049/051 测规则侧计算；golden_052 测执行写台账；golden_055 测趋势判定。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, LedgerType, Role
from app.engine.factors import InventoryProfile
from app.models.identity import Account
from app.models.job import Ledger
from app.services.relocate import build_relocate_plans, confirm_relocate, derive_consolidation_plan
from evals.eval_utils import trend
from tests.logic.conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.l3

WAREHOUSE = "GTJ10036"
MATERIAL = "M1"
BATCH = "B26090801"
OTHER = "B26090802"
NOW = datetime(2026, 9, 14, 10, 0)

#: doc 10 §六 的示例分布：12 巷 / 340 板，板数降序（与 tests/logic/test_relocate_propose.py 同源）。
_DOC_PLATES = {
    "01": 85, "05": 60, "08": 45, "12": 38, "15": 30,
    "21": 25, "22": 22, "25": 15, "28": 10, "31": 5, "35": 3, "42": 2,
}


def _plenty() -> dict[str, int]:
    return {aisle: 1000 for aisle in _DOC_PLATES}


def _batch_locations(aisle_qtys: dict[str, int]) -> dict[str, list[tuple[str, int]]]:
    """批号级逐格库位：每巷一格（`{aisle}0101`），箱数 = 该批在该巷的箱数（缺口 2 真实库位）。"""
    return {aisle: [(f"{aisle}0101", qty)] for aisle, qty in aisle_qtys.items()}


def _material_locations(aisles) -> dict[str, list[str]]:
    """物料级库位：每巷一个既有库位（`{aisle}0101`），作为目标库位候选（缺口 2）。"""
    return {aisle: [f"{aisle}0101"] for aisle in aisles}


def _operator(session: Session) -> Account:
    account = Account(
        warehouse_id=WAREHOUSE,
        username="gtj_keeper",
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    return account


def test_golden_049_relocate_plans_three_tiers_with_quantified_cost():
    """golden_049：三档方案（激进/均衡/保守）+ 量化代价（板数/车次/时长）正确。"""
    plans = build_relocate_plans(plates_by_aisle=_DOC_PLATES, available=_plenty())

    assert [p["name"] for p in plans["plans"]] == ["激进", "均衡", "保守"]
    assert [p["cross_aisle"]["after"] for p in plans["plans"]] == [2, 5, 8]
    aggressive = plans["plans"][0]
    assert aggressive["target_aisles"] == ["01", "05"]
    assert aggressive["plates_to_move"] == 195
    assert aggressive["trips"] == 13
    assert aggressive["hours"] == pytest.approx(2.6)


def test_golden_051_triple_validation_required_for_adoption():
    """golden_051：三重校验（cap 充足 / 批号不变 / 集中度改善）通过才采纳。"""
    # 缺口 1 的物料级口径：本批独占 03（该巷批号集 = {BATCH}），收拢后 03 变空；
    # 02 与 OTHER 共占，收拢后不变空 → after = 3 − 1 = 2。
    ok_profile = InventoryProfile(
        snapshot_present=True,
        plates_by_aisle={"01": 30, "02": 20, "03": 10},
        batches_by_aisle={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
    )
    material_locations = _material_locations(["01", "02", "03"])

    # 三重校验全过 → 出方案（采纳），且批号不变、跨巷道下降。
    ok = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=ok_profile,
        batch_plates_by_aisle={"02": 10, "03": 10},
        available={"01": 100, "02": 100, "03": 100},
        batch_locations_by_aisle=_batch_locations({"02": 10, "03": 10}),
        material_locations_by_aisle=material_locations,
    )
    assert ok.plan is not None
    assert ok.plan["batch_unchanged"] is True
    assert ok.plan["expected_cross_aisle"]["after"] < ok.plan["expected_cross_aisle"]["before"]

    # cap 不足（主巷 + 次选均不足）→ 不采纳（移出批量）。
    starved = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=InventoryProfile(
            snapshot_present=True,
            plates_by_aisle={"01": 30, "02": 20, "03": 10},
            batches_by_aisle={"01": {BATCH, OTHER}, "02": {OTHER}, "03": {BATCH}},
        ),
        batch_plates_by_aisle={"01": 5, "03": 10},
        available={"01": 5, "02": 5, "03": 100},
        batch_locations_by_aisle=_batch_locations({"01": 5, "03": 10}),
        material_locations_by_aisle=material_locations,
    )
    assert starved.plan is None
    assert starved.moved_out_reason is not None


def test_golden_052_relocate_execution_updates_aisle_keeps_batch(session):
    """golden_052：移库执行 → 台账更新巷道（源 + 目标），批号不变（红线）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec("010104", MATERIAL, BATCH, 40)],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01", material_code=MATERIAL, qty=40, batch_no=BATCH,
                job_type=JobType.RELOCATE, status=JobStatus.PLANNED,
            ),
        ],
    )
    order = scenario.job_orders[0]

    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_locations=[{"location_code": "010104", "qty": 40}],
        target_location_code="010105",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    ledger = session.scalars(
        select(Ledger).where(Ledger.job_order_id == order.id).order_by(Ledger.id)
    ).first()
    assert ledger.ledger_type is LedgerType.RELOCATE
    assert ledger.source_location_code == "010104"
    assert ledger.target_location_code == "010105"
    assert ledger.batch_no == BATCH  # 移库不改批号（红线）


def test_golden_055_relocate_reduces_cross_aisle():
    """golden_055：移库后同物料跨巷道下降（趋势向好）。"""
    plan = derive_consolidation_plan(
        material_code=MATERIAL,
        batch_no=BATCH,
        profile=InventoryProfile(
            snapshot_present=True,
            plates_by_aisle={"01": 30, "02": 20, "03": 10},
            batches_by_aisle={"01": {OTHER}, "02": {BATCH, OTHER}, "03": {BATCH}},
        ),
        batch_plates_by_aisle={"02": 10, "03": 10},
        available={"01": 100, "02": 100, "03": 100},
        batch_locations_by_aisle=_batch_locations({"02": 10, "03": 10}),
        material_locations_by_aisle=_material_locations(["01", "02", "03"]),
    )
    assert plan.plan is not None
    before = plan.plan["expected_cross_aisle"]["before"]  # 3
    after = plan.plan["expected_cross_aisle"]["after"]    # 2
    assert after < before  # 移库后跨巷道下降
    assert trend(after, before, higher_is_better=False) == "better"  # 趋势向好
