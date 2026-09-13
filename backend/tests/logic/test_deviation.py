"""偏离标记的契约测试（tasks.md 4.3 的验证）。

事实来源：17-数据模型设计 §4.4（`Deviation` 实体 / 成因分类 / 状态）
          spec `transaction-base`「偏离批次标记」（场景：偏离写入 Deviation / 达标不写 Deviation）
          design.md D5（编排链：后验记录 → 偏离记录，同一事务）

三条要钉住的口径：

  1. **偏离写**：`verify_result = DEVIATION` 的指标落一条 `Deviation`，标识 = 物料 + 批号，
     `actual/threshold_cross_aisle` 取该指标自身的实测 / 阈值，成因按作业类型给 v1 默认值。
  2. **达标不写**：`verify_result = PASS` 的作业单不产生任何 `Deviation` 记录。
  3. **成因分派**：入库偏离 → 新入库收拢不达标；移库偏离 → 历史库存拖累（v1 默认，
     操作员可后续修订，`15-04` §4.1）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, Role
from app.models.identity import Account
from app.models.job import Deviation, DeviationCauseKind, DeviationStatus
from app.services.inbound import confirm_inbound
from app.services.relocate import confirm_relocate

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


def _deviations(session: Session) -> list[Deviation]:
    return list(session.scalars(select(Deviation).order_by(Deviation.id)))


# ------------------------------------------------------------------ 偏离写入 Deviation

def test_inbound_deviation_writes_deviation(session: Session) -> None:
    """入库把料落进第 6 条巷道（同批仍 ≤3）→ 一条 `Deviation`：物料口径 6>5、成因新入库收拢不达标。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 6)
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
        target_location_code="060101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    deviations = _deviations(session)
    assert len(deviations) == 1
    dev = deviations[0]
    assert dev.warehouse_id == WAREHOUSE
    assert dev.material_code == MATERIAL
    assert dev.batch_no == BATCH
    assert dev.actual_cross_aisle == 6
    assert dev.threshold_cross_aisle == 5
    assert dev.cause_kind is DeviationCauseKind.NEW_INBOUND_SHORTFALL
    assert dev.status is DeviationStatus.OPEN


# ------------------------------------------------------------------ 达标不写 Deviation

def test_inbound_pass_writes_no_deviation(session: Session) -> None:
    """入库落位收拢（1 条巷道）→ `VERIFIED` 但**不产生** `Deviation` 记录。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
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
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    assert _deviations(session) == []


# ------------------------------------------------------------------ 成因分派（移库 → 历史库存拖累）

def test_relocate_deviation_writes_legacy_cause(session: Session) -> None:
    """移库未收拢（跨巷道 2→2 持平）→ 一条 `Deviation`，成因历史库存拖累。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40),
            InventorySpec(location_code="010105", material_code=MATERIAL, batch_no=BATCH, qty=40),
            InventorySpec(location_code="020101", material_code=MATERIAL, batch_no=BATCH, qty=40),
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_location_code="010104",
        target_location_code="020101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    deviations = _deviations(session)
    assert len(deviations) == 1
    dev = deviations[0]
    assert dev.actual_cross_aisle == 2
    assert dev.threshold_cross_aisle == 2
    assert dev.cause_kind is DeviationCauseKind.LEGACY_INVENTORY_DRAG
