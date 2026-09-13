"""KPI 计量正确性（基线层维度，tasks.md 7.2 的验证）。

事实来源：20-评测体系设计 §六 表 2（后验三口径与阈值）、§1.3（逐单 vs 聚合口径）
          15-01 §8.1（后验口径）、15-00 §六（阈值表）
          spec `transaction-base`「同步后验与三口径判定」

钉的是**量**对不对，不是**判**对不对（判定在 `test_verify.py`）。三条口径的「计量」：

  1. **跨巷道数是去重后的巷道数，不是库位数**：一个料铺在 N 个库位、但只跨 M 条巷道时，
     计量是 M。库位按 `[:2]` 切巷道（`CLAUDE.md` §七），同巷多个库位只算一条。
  2. **拣货量加权集中度** = 按量降序累加到 80% 覆盖的巷道数；80% 恰好的边界取值要正确。
  3. **Deviation 的 actual / threshold 与该指标自身的实测 / 阈值同源同值**（整数化），
     与 `Verification` 不另起一套 —— 阈值 5（同物料）/ 3（同批）可区分两行偏离。

与 `test_verify.py` 的分野：那边用**手工构造的 `SnapshotIndex`** 钉判定；这边用**真实库存行
经 `load_snapshot_index` 聚合**钉「数对了没」—— 5 个库位 2 条巷道，计量是 2 不是 5。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, Role, VerifyResult
from app.engine.factors import load_snapshot_index
from app.models.identity import Account
from app.models.job import Deviation, Verification
from app.services.inbound import confirm_inbound
from app.services.verify import concentration_aisle_count, verify_inbound

from .conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"
MATERIAL = "M1"


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


# ------------------------------------------------------------------ 跨巷道：去重后的巷道数，不是库位数

def test_material_cross_aisle_counts_distinct_aisles_not_locations(session: Session) -> None:
    """5 个库位、2 条巷道 → 同物料跨巷道 = 2（去重后），不是 5。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010101", material_code=MATERIAL, batch_no="LEGACY", qty=10),
            InventorySpec(location_code="010102", material_code=MATERIAL, batch_no="LEGACY", qty=10),
            InventorySpec(location_code="010103", material_code=MATERIAL, batch_no="LEGACY", qty=10),
            InventorySpec(location_code="020101", material_code=MATERIAL, batch_no="LEGACY", qty=10),
            InventorySpec(location_code="020102", material_code=MATERIAL, batch_no="LEGACY", qty=10),
        ],
    )

    index = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)
    assert index.profile(MATERIAL).cross_aisle_count == 2

    material, _batch = verify_inbound(material_code=MATERIAL, batch_no=BATCH, snapshot=index)
    assert material.metric_kind == "同物料跨巷道"
    assert material.actual_value == 2  # 同巷多个库位只算一条


def test_batch_cross_aisle_counts_distinct_aisles(session: Session) -> None:
    """同批铺在 3 个库位、2 条巷道 → 同批跨巷道 = 2。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010101", material_code=MATERIAL, batch_no=BATCH, qty=10),
            InventorySpec(location_code="010102", material_code=MATERIAL, batch_no=BATCH, qty=10),
            InventorySpec(location_code="020101", material_code=MATERIAL, batch_no=BATCH, qty=10),
        ],
    )

    index = load_snapshot_index(session, snapshot_id=scenario.snapshot.id)
    _material, batch = verify_inbound(material_code=MATERIAL, batch_no=BATCH, snapshot=index)
    assert batch.metric_kind == "同批跨巷道"
    assert batch.actual_value == 2


# ------------------------------------------------------------------ 加权集中度：80% 边界

def test_concentration_exact_80_percent_boundary() -> None:
    """80/20：80 恰是 100 的 80% → 覆盖 1 条巷道。79/21：79 < 80，加 21 到 100 → 2 条。"""
    assert concentration_aisle_count(pick_qty_by_aisle={"01": 80, "02": 20}) == 1
    assert concentration_aisle_count(pick_qty_by_aisle={"01": 79, "02": 21}) == 2


def test_concentration_equal_spread_requires_more_aisles() -> None:
    """4 条巷道各 25：累加 25/50/75/100，到 100 才 ≥ 80 → 覆盖 4 条。"""
    assert (
        concentration_aisle_count(pick_qty_by_aisle={"01": 25, "02": 25, "03": 25, "04": 25})
        == 4
    )


# ------------------------------------------------------------------ Deviation 阈值计量：与指标同源同值

def test_batch_deviation_records_threshold_3(session: Session) -> None:
    """同批已跨 4 巷道（>3）→ 一条 `Deviation`，`threshold_cross_aisle = 3`（同批口径的
    阈值，不是同物料的 5），且与 `Verification` 的实测/阈值同源同值。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=10)
            for i in range(1, 5)  # 同批已铺 4 条巷道
        ],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="010104",  # 落回已铺批号的巷道，不新增巷道
        snapshot=scenario.snapshot,
    )

    # 同物料仍 4 巷道（≤5，PASS）；同批 4 巷道（>3，DEVIATION）→ 只一条偏离，阈值 3。
    deviations = list(session.scalars(select(Deviation)))
    assert len(deviations) == 1
    dev = deviations[0]
    assert dev.actual_cross_aisle == 4
    assert dev.threshold_cross_aisle == 3

    verifications = {v.metric_kind: v for v in session.scalars(select(Verification))}
    batch_metric = verifications["同批跨巷道"]
    assert batch_metric.verify_result is VerifyResult.DEVIATION
    assert batch_metric.actual_value == 4
    assert dev.actual_cross_aisle == int(batch_metric.actual_value)
    assert dev.threshold_cross_aisle == int(batch_metric.threshold_value)
