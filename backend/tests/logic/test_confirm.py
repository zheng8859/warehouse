"""三类作业确认→执行编排的契约测试（tasks.md 3.3 的验证）。

事实来源：15-入库出库移库与后验流程设计 §6.2/§6.3、§3.1（状态机）
          17-数据模型设计 §4.1/§4.5
          openspec/changes/transaction-base/design.md D5 / D7
          spec `transaction-base`「三类作业确认与落位执行」

三条要钉住的口径：

  1. **成功链路**：`PLANNED → CONFIRMED → EXECUTED → VERIFIED`，同事务写出台账（按
     `_LEDGER_LOCATION_CHECK` 的矩阵）+ cap 增量（入库目标 +、出库源 −、移库源 − 目标 +），
     确认痕迹（确认人 / 时刻 / 实际落位 / 执行时刻）落库，后验在同请求内同步接上。
  2. **写台账失败回退 `PLANNED`**：任一步失败 savepoint 整体回滚，单子经
     `CONFIRMED → PLANNED` 回边退回已出方案、确认痕迹清空、**不产生任何台账**。
  3. **状态守卫**：非 `PLANNED` 的单（`PENDING` / 已 `VERIFIED`）确认即抛 `StateConflict`
     **上抛**（不是「执行失败」，不该被降级吞掉）—— 幂等靠它兑现，`VERIFIED` 后重复确认
     写不出第二条台账。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, LedgerType, Role
from app.core.errors import StateConflict
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.models.linkage import InventoryItem
from app.services.inbound import confirm_inbound
from app.services.outbound import confirm_outbound
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


# ------------------------------------------------------------------ 成功链路

def test_confirm_inbound_success(session: Session) -> None:
    """入库确认：`PLANNED → EXECUTED`，无源有目标台账 + 目标库位 cap 增量。"""
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

    result = confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert result is order
    assert order.status is JobStatus.VERIFIED
    assert order.confirmed_by_id == operator.id
    assert order.confirmed_at == NOW
    assert order.actual_location_code == "010104"
    assert order.actual_qty == 40
    assert order.executed_at == NOW

    ledgers = _ledgers(session, order)
    assert len(ledgers) == 1
    assert ledgers[0].ledger_type is LedgerType.INBOUND
    assert ledgers[0].source_location_code is None
    assert ledgers[0].target_location_code == "010104"
    assert ledgers[0].operator_id == operator.id

    assert _qty_at(session, scenario.snapshot.id, "010104") == 40


def test_confirm_outbound_success(session: Session) -> None:
    """出库确认：有源无目标台账 + 源库位 cap 减量。"""
    operator = _operator(session)
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
        operator_id=operator.id,
        executed_at=NOW,
        source_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    assert order.actual_location_code == "010104"
    ledger = _ledgers(session, order)[0]
    assert ledger.ledger_type is LedgerType.OUTBOUND
    assert ledger.source_location_code == "010104"
    assert ledger.target_location_code is None
    assert _qty_at(session, scenario.snapshot.id, "010104") == 10


def test_confirm_relocate_success(session: Session) -> None:
    """移库确认：源 + 目标台账 + 源减目标增（批号不变）。"""
    operator = _operator(session)
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
        operator_id=operator.id,
        executed_at=NOW,
        source_location_code="010104",
        target_location_code="010105",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    ledger = _ledgers(session, order)[0]
    assert ledger.ledger_type is LedgerType.RELOCATE
    assert ledger.source_location_code == "010104"
    assert ledger.target_location_code == "010105"
    assert ledger.batch_no == BATCH  # 移库不改批号
    assert _qty_at(session, scenario.snapshot.id, "010104") is None
    assert _qty_at(session, scenario.snapshot.id, "010105") == 40


# ------------------------------------------------------------------ 失败回退 PLANNED

def test_confirm_failure_returns_to_planned(session: Session) -> None:
    """写台账失败（无批号）→ 回边 `PLANNED`、确认痕迹清空、不产生台账。

    对应 spec 场景「写台账失败回退到 PLANNED」：确认作废、须重新确认。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=None,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    result = confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert result is order
    assert order.status is JobStatus.PLANNED
    assert order.confirmed_by_id is None
    assert order.confirmed_at is None
    assert order.actual_location_code is None
    assert order.actual_qty is None
    assert _ledgers(session, order) == []
    assert _qty_at(session, scenario.snapshot.id, "010104") is None


# ------------------------------------------------------------------ 状态守卫（幂等）

def test_confirm_pending_order_is_rejected(session: Session) -> None:
    """非 `PLANNED` 的单（`PENDING`）确认 → `StateConflict` 上抛（不是执行失败）。"""
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
                status=JobStatus.PENDING,
            )
        ],
    )
    order = scenario.job_orders[0]

    with pytest.raises(StateConflict):
        confirm_inbound(
            session,
            job_order=order,
            operator_id=operator.id,
            executed_at=NOW,
            target_location_code="010104",
            snapshot=scenario.snapshot,
        )
    assert _ledgers(session, order) == []


def test_confirm_verified_order_is_rejected(session: Session) -> None:
    """`VERIFIED` 后重复确认 → `StateConflict`，写不出第二条台账（15 §11.7 / D7）。"""
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

    with pytest.raises(StateConflict):
        confirm_inbound(
            session,
            job_order=order,
            operator_id=operator.id,
            executed_at=NOW,
            target_location_code="010105",
            snapshot=scenario.snapshot,
        )
    assert len(_ledgers(session, order)) == 1
