"""冲正守卫的契约测试（tasks.md 5.2 的验证）。

事实来源：spec `transaction-base`「冲正回冲」、specs/data-model/spec.md「JobOrder 状态机」
          （仅 `EXECUTED → VOID`、`VERIFIED → VOID` 两条冲正边）
          app/core/state_machine.py（LEGAL_TRANSITIONS）

四条要钉住的口径：

  1. **仅 `EXECUTED` / `VERIFIED` 可冲正**：其余源状态冲正一律 `StateConflict` 上抛。
  2. **`VOID` 后不可再冲正**：`VOID` 是终态，重复冲正被拒（幂等）。
  3. **`VERIFY_FAILED` 不可直接冲正**：后验失败只有重试边，要先重试到 `VERIFIED` 才能冲正
     （「无放弃后验终态」—— 冲正边不开给一个后验还没走完的单）。
  4. **守卫失败不产生任何反向台账**：`assert_transition` 在 savepoint 之外、写台账之前，
     非法源状态连正常台账都还没有，更不该冒出反向行。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, Role
from app.core.errors import StateConflict
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.services.inbound import confirm_inbound
from app.services.void import void_job

from .conftest import JobOrderSpec, make_scenario

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


# ------------------------------------------------------------------ 非 EXECUTED / VERIFIED 被拒

def test_void_planned_is_rejected(session: Session) -> None:
    """`PLANNED`（未确认 / 未执行）冲正 → `StateConflict`，不产生任何台账。"""
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

    with pytest.raises(StateConflict):
        void_job(
            session,
            job_order=order,
            operator_id=operator.id,
            voided_at=NOW,
            snapshot=scenario.snapshot,
        )

    assert order.status is JobStatus.PLANNED
    assert _ledgers(session, order) == []


def test_void_pending_is_rejected(session: Session) -> None:
    """`PENDING`（未出方案）冲正 → `StateConflict`。"""
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
        void_job(
            session,
            job_order=order,
            operator_id=operator.id,
            voided_at=NOW,
            snapshot=scenario.snapshot,
        )
    assert order.status is JobStatus.PENDING


def test_void_verify_failed_is_rejected(session: Session) -> None:
    """`VERIFY_FAILED` 冲正 → `StateConflict`：后验失败只有重试边，须先重试到 `VERIFIED`。

    快照缺失现在在确认处就**阻断**（见 `test_verify_flow`），不再经由 `VERIFY_FAILED`
    表达 —— 故这里直接置 `VERIFY_FAILED`，钉「后验失败不可冲正」而非「如何进入」。
    """
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
    order.status = JobStatus.VERIFY_FAILED
    session.flush()

    with pytest.raises(StateConflict):
        void_job(
            session,
            job_order=order,
            operator_id=operator.id,
            voided_at=NOW,
            snapshot=scenario.snapshot,
        )
    assert order.status is JobStatus.VERIFY_FAILED


# ------------------------------------------------------------------ VOID 后不可再冲正

def test_void_voided_is_rejected(session: Session) -> None:
    """已冲正（`VOID`）再冲正 → `StateConflict`，仍只有一正常行 + 一反向行。"""
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
    void_job(
        session,
        job_order=order,
        operator_id=operator.id,
        voided_at=NOW,
        snapshot=scenario.snapshot,
    )
    assert order.status is JobStatus.VOID
    assert [lg.is_reversal for lg in _ledgers(session, order)] == [False, True]

    with pytest.raises(StateConflict):
        void_job(
            session,
            job_order=order,
            operator_id=operator.id,
            voided_at=NOW,
            snapshot=scenario.snapshot,
        )
    # 唯一约束兜底也不该被撞到 —— 守卫在写台账之前就拦下了，行数不变。
    assert [lg.is_reversal for lg in _ledgers(session, order)] == [False, True]
