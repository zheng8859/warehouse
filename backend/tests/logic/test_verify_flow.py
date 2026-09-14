"""后验编排的契约测试（tasks.md 4.2 的验证）。

事实来源：15-01 §3.3.2（VERIFYING 不对外停留 / VERIFY_FAILED 仅重试+告警）、§8.1
          15-03 §8.1 / 15-04 §8.1（三类口径）
          spec `transaction-base`「同步后验与三口径判定」（场景：后验失败可重试）
          design.md D2（后验同步，VERIFYING 为内部中间态）

三条要钉住的口径：

  1. **确认请求结束必为 `VERIFIED` / `VERIFY_FAILED`**：`VERIFYING` 是内部中间态，确认链
     走完 `EXECUTED → VERIFYING → VERIFIED / VERIFY_FAILED` 一次到底，不对外停留。
  2. **后验结果落 `Verification`**：一行一条指标（入库两条 / 出库一条 / 移库一条），
     达标 / 偏离都由 `verify_result` 标记。
  3. **后验失败可重试**：`VERIFY_FAILED → VERIFYING → VERIFIED` 是唯一重试边，无放弃后验
     终态；后验失败**不推翻**已写入的台账（台账已正确，后验仅作度量）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, Role, VerifyResult
from app.core.errors import BlockedMissingPrerequisite
from app.models.identity import Account
from app.models.job import JobOrder, Ledger, Verification
from app.services.inbound import confirm_inbound
from app.services.outbound import confirm_outbound
from app.services.relocate import confirm_relocate
from app.services.verify import run_verification

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


def _verifications(session: Session, order: JobOrder) -> list[Verification]:
    return list(
        session.scalars(
            select(Verification)
            .where(Verification.job_order_id == order.id)
            .order_by(Verification.id)
        )
    )


def _ledgers(session: Session, order: JobOrder) -> list[Ledger]:
    return list(
        session.scalars(
            select(Ledger).where(Ledger.job_order_id == order.id).order_by(Ledger.id)
        )
    )


# ------------------------------------------------------------------ 确认请求结束必为 VERIFIED / VERIFY_FAILED

def test_confirm_inbound_verifies_pass(session: Session) -> None:
    """入库确认后验达标：`VERIFIED` + 两条 `Verification`（同物料 / 同批跨巷道 PASS）。"""
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
    verifications = _verifications(session, order)
    assert [v.metric_kind for v in verifications] == ["同物料跨巷道", "同批跨巷道"]
    assert all(v.verify_result is VerifyResult.PASS for v in verifications)
    assert [v.actual_value for v in verifications] == [1, 1]


def test_confirm_inbound_verifies_deviation(session: Session) -> None:
    """入库落进第 6 条巷道 → `VERIFIED`，同物料跨巷道口径 `DEVIATION`（>5）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=10
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
    by_kind = {v.metric_kind: v for v in _verifications(session, order)}
    assert by_kind["同物料跨巷道"].verify_result is VerifyResult.DEVIATION
    assert by_kind["同物料跨巷道"].actual_value == 6


def test_confirm_inbound_without_snapshot_blocked(session: Session) -> None:
    """无快照 → 确认**阻断**（`BlockedMissingPrerequisite`，409），不写台账、不迁 `VERIFY_FAILED`。"""
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
        snapshot_time=None,
    )
    order = scenario.job_orders[0]

    with pytest.raises(BlockedMissingPrerequisite):
        confirm_inbound(
            session,
            job_order=order,
            operator_id=operator.id,
            executed_at=NOW,
            target_location_code="010104",
            snapshot=None,
        )

    assert order.status is JobStatus.PLANNED  # 阻断发生在迁移之前，单子仍在 PLANNED
    assert _ledgers(session, order) == []
    assert _verifications(session, order) == []


# ------------------------------------------------------------------ 失败重试边（无放弃后验终态）

def test_verify_failed_retries_to_verified(session: Session) -> None:
    """`VERIFY_FAILED → VERIFYING → VERIFIED`：重试即 `run_verification`。

    快照缺失现在在确认处就**阻断**（见上一个用例），不再经由 `VERIFY_FAILED` 表达 ——
    故这里直接置 `VERIFY_FAILED`（模拟此前某次后验因数据矛盾失败），补快照后重试，
    钉的是「重试边」而非「如何进入 VERIFY_FAILED」。
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

    run_verification(session, job_order=order, snapshot=scenario.snapshot)

    assert order.status is JobStatus.VERIFIED
    assert len(_verifications(session, order)) == 2


# ------------------------------------------------------------------ 出库 / 移库单条指标

def test_confirm_outbound_verifies_concentration(session: Session) -> None:
    """出库确认 → `VERIFIED`，一条「拣货量加权集中度」指标（单巷 → 恒 1 → PASS）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=50
            )
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
        pick_path_json=[{"aisle": "01", "qty": 40, "batches": [BATCH]}],
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    verifications = _verifications(session, order)
    assert [v.metric_kind for v in verifications] == ["拣货量加权集中度"]
    assert verifications[0].verify_result is VerifyResult.PASS
    assert verifications[0].actual_value == 1


def test_confirm_relocate_verifies_drop(session: Session) -> None:
    """移库把两条巷道并回一条 → `VERIFIED`，一条「同物料跨巷道是否下降」指标（2→1 PASS）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40
            ),
            InventorySpec(
                location_code="020101", material_code=MATERIAL, batch_no=BATCH, qty=40
            ),
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
        source_location_code="020101",
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    verifications = _verifications(session, order)
    assert [v.metric_kind for v in verifications] == ["同物料跨巷道是否下降"]
    assert verifications[0].verify_result is VerifyResult.PASS
    assert verifications[0].actual_value == 1
    assert verifications[0].threshold_value == 2
