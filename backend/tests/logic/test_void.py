"""冲正编排的契约测试（tasks.md 5.1 的验证）。

事实来源：17-数据模型设计 §4.3（`Ledger.is_reversal` 反向行）
          openspec/changes/transaction-base/design.md D3（冲正 v1 简化）
          spec `transaction-base`「冲正回冲」（场景：冲正置 VOID 并回冲）

四条要钉住的口径：

  1. **VOID 终态**：冲正后单子落 `VOID`（终态，不可再迁）。
  2. **反向台账行**：写一条 `is_reversal=True` 的反向行，source/target 照抄原正常行，
     `ledger_type` 与原行一致 —— 一单至多一正常行 + 一反向行。
  3. **cap / 库存回补**：反向行经 `apply_increment` 取反 —— 入库目标减、出库源增、
     移库源增目标减（冲正把确认时的增量原样抹回）。
  4. **无反向 `JobOrder`**：冲正不创建反向作业单，`job_orders` 仍只有原单。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, LedgerType, Role
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.models.linkage import InventoryItem
from app.services.inbound import confirm_inbound
from app.services.outbound import confirm_outbound
from app.services.relocate import confirm_relocate
from app.services.void import void_job

from .conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"
MATERIAL = "M1"


def _operator(session: Session, username: str) -> Account:
    account = Account(
        warehouse_id=WAREHOUSE,
        username=username,
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    return account


def _ledgers(session: Session, order: JobOrder) -> list[Ledger]:
    return list(
        session.scalars(
            select(Ledger).where(Ledger.job_order_id == order.id).order_by(Ledger.id)
        )
    )


def _qty_at(session: Session, snapshot_id: int, location_code: str) -> int | None:
    item = session.scalars(
        select(InventoryItem).where(
            InventoryItem.snapshot_id == snapshot_id,
            InventoryItem.location_code == location_code,
            InventoryItem.batch_no == BATCH,
            InventoryItem.material_code == MATERIAL,
        )
    ).first()
    return None if item is None else item.qty


def _job_order_count(session: Session) -> int:
    return len(list(session.scalars(select(JobOrder))))


# ------------------------------------------------------------------ 入库冲正：目标库位回补

def test_void_inbound_reverses(session: Session) -> None:
    """入库确认后冲正：VOID 终态 + 反向行 + 目标库位库存抹回 + 无反向作业单。"""
    confirmer = _operator(session, "gtj_confirmer")
    voider = _operator(session, "gtj_voider")
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
        operator_id=confirmer.id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )
    assert order.status is JobStatus.VERIFIED
    assert _qty_at(session, scenario.snapshot.id, "010104") == 40

    void_job(
        session,
        job_order=order,
        operator_id=voider.id,
        voided_at=NOW,
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VOID
    ledgers = _ledgers(session, order)
    assert [lg.is_reversal for lg in ledgers] == [False, True]
    normal, reverse = ledgers
    assert normal.ledger_type is LedgerType.INBOUND
    assert normal.operator_id == confirmer.id
    assert reverse.ledger_type is LedgerType.INBOUND
    assert reverse.source_location_code is None
    assert reverse.target_location_code == "010104"
    assert reverse.operator_id == voider.id
    # 目标库位入库 40 → 冲正抹回 0（行删除）
    assert _qty_at(session, scenario.snapshot.id, "010104") is None
    # 冲正不创建反向 JobOrder
    assert _job_order_count(session) == 1


# ------------------------------------------------------------------ 出库冲正：源库位回补

def test_void_outbound_reverses(session: Session) -> None:
    """出库确认后冲正：按拣货路径回补，库存由 10 回到 50。"""
    voider = _operator(session, "gtj_voider")
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=50)
        ],
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    confirm_outbound(
        session,
        job_order=order,
        operator_id=voider.id,
        executed_at=NOW,
        pick_path_json=[{"aisle": "01", "qty": 40, "batches": [BATCH]}],
        snapshot=scenario.snapshot,
    )
    assert _qty_at(session, scenario.snapshot.id, "010104") == 10

    void_job(
        session,
        job_order=order,
        operator_id=voider.id,
        voided_at=NOW,
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VOID
    reverse = _ledgers(session, order)[1]
    assert reverse.ledger_type is LedgerType.OUTBOUND
    assert reverse.source_location_code is None
    assert reverse.target_location_code is None
    assert reverse.pick_path_json == [{"aisle": "01", "qty": 40, "batches": [BATCH]}]
    assert _qty_at(session, scenario.snapshot.id, "010104") == 50


# ------------------------------------------------------------------ 移库冲正：源回补、目标抹回

def test_void_relocate_reverses(session: Session) -> None:
    """移库确认后冲正：源库位回补 40、目标库位抹回 0（批号不变）。"""
    voider = _operator(session, "gtj_voider")
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40)
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
        operator_id=voider.id,
        executed_at=NOW,
        source_location_code="010104",
        target_location_code="010105",
        snapshot=scenario.snapshot,
    )
    assert _qty_at(session, scenario.snapshot.id, "010104") is None
    assert _qty_at(session, scenario.snapshot.id, "010105") == 40

    void_job(
        session,
        job_order=order,
        operator_id=voider.id,
        voided_at=NOW,
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VOID
    reverse = _ledgers(session, order)[1]
    assert reverse.ledger_type is LedgerType.RELOCATE
    assert reverse.source_location_code == "010104"
    assert reverse.target_location_code == "010105"
    assert reverse.batch_no == BATCH  # 移库冲正同样不改批号
    assert _qty_at(session, scenario.snapshot.id, "010104") == 40
    assert _qty_at(session, scenario.snapshot.id, "010105") is None
