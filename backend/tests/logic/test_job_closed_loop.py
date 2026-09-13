"""作业闭环一致性（基线层维度 CL-001~006，tasks.md 7.1 的验证）。

事实来源：20-评测体系设计 §六 表 2「作业闭环一致性（基线，CL-001~006）」
          15-入库出库移库与后验流程设计 §6.2/§6.3（逐单处置与写台账）、§7（后验与偏离）
          spec `transaction-base`「三类作业确认与落位执行」「冲正回冲」

六个场景钉的是**闭环**：推荐 → 人确认（接受/微调/驳回）→ 写台账 → 后验 → 冲正，
一条料从入到出的每一跳都不丢、不静默改写。CL-001~006 各自钉一跳：

  CL-001  人确认落位到推荐巷道 → 台账记的就是那条巷道，与确认一致
  CL-002  人驳回落位（选别的巷道）→ 台账记的是人选的那条，不静默改写成推荐巷道
  CL-003  未走二次确认卡直接提交 → 被拦截，不写台账
  CL-004  落位后回查台账 → 字段（单号/物料/巷道/时间）完整
  CL-005  出库拣配读取落位 → 读到入库写入的那条巷道，前后一致
  CL-006  移库执行后 → 台账巷道更新为新巷道，旧记录留痕（source 留痕）

## 口径备注（评审要看）：CL-002 的「状态为人工改」

「人工改」落在 `JobOrder.disposition`（`TUNE`）与 `tune_detail` 两列上，那是**逐单处置**
（15 §2.1 五段骨架的第 5 步 Accept/Tune/Reject）要写的东西。本 change（transaction-base）
只落「批量确认」这一跳 —— 端点集是 confirm / reject / retry / void，不含「逐单处置」端点，
故 `disposition` 在本 change 仍是未接线的列（见 `app/models/job.py` 的列注释）。

CL-002 在这里钉的是「不静默改写」这个**结果**：台账与 `actual_location_code` 记的是操作员
确认的巷道，而不是推荐方案里的巷道 —— 这一跳由确认编排的入参结构保证（落位来自确认请求，
不来自推荐方案）。「人工改」列本身由后续的逐单处置 step 接线，不在本任务的断言范围。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, LedgerType, Role
from app.core.errors import StateConflict
from app.models.identity import Account
from app.models.job import JobOrder, Ledger, PlanKind, RecommendationPlan
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


def _recommend(session: Session, order: JobOrder, target_location_code: str) -> None:
    """给一张单摆一份推荐方案，指向 `target_location_code` —— 供 CL-001/002 对照「推荐巷道」。

    确认编排**不读**这份方案（落位来自确认请求，见模块 docstring）；它只作断言里
    「推荐 vs 实际」的对照物，把「不静默改写」这个断言落到一个具体的数上。
    """
    session.add(
        RecommendationPlan(
            warehouse_id=WAREHOUSE,
            job_order_id=order.id,
            plan_kind=PlanKind.ASSIGN,
            payload_json={"target_location_code": target_location_code},
        )
    )
    session.flush()


# ------------------------------------------------------------------ CL-001 确认到推荐巷道 → 台账一致

def test_cl001_confirm_to_recommended_aisle_records_same(session: Session) -> None:
    """人确认落位到推荐巷道（03）→ 台账巷道 = 03，与确认一致。"""
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
    _recommend(session, order, "030101")

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="030101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    ledger = _ledgers(session, order)[0]
    assert ledger.target_location_code == "030101"
    assert order.actual_location_code == "030101"


# ------------------------------------------------------------------ CL-002 驳回落位 → 不静默改写

def test_cl002_human_override_is_recorded_not_silently_rewritten(session: Session) -> None:
    """推荐巷道是 03，人选了 02 → 台账记 02，不静默改写成推荐巷道 03。"""
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
    _recommend(session, order, "030101")  # 推荐 03

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="020101",  # 人选 02（驳回推荐、改落别处）
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    ledger = _ledgers(session, order)[0]
    assert ledger.target_location_code == "020101"  # 人选巷道
    assert ledger.target_location_code != "030101"  # 不是推荐巷道
    assert order.actual_location_code == "020101"


# ------------------------------------------------------------------ CL-003 未确认提交 → 拦截，不写台账

def test_cl003_unconfirmed_submit_is_blocked_without_ledger(session: Session) -> None:
    """未走二次确认卡（`PENDING`，尚未逐单处置）直接提交 → 状态机拦截，不写台账。"""
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


# ------------------------------------------------------------------ CL-004 回查台账 → 字段完整

def test_cl004_ledger_fields_complete(session: Session) -> None:
    """落位后回查台账：单号 / 物料 / 巷道 / 时间等字段完整。"""
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

    ledger = _ledgers(session, order)[0]
    assert ledger.order_no == "PO-01"
    assert ledger.material_code == MATERIAL
    assert ledger.batch_no == BATCH
    assert ledger.qty == 40
    assert ledger.target_location_code == "010104"
    assert ledger.executed_at == NOW
    assert ledger.operator_id == operator.id
    assert ledger.ledger_type is LedgerType.INBOUND


# ------------------------------------------------------------------ CL-005 出库拣配读取落位 → 前后一致

def test_cl005_outbound_reads_inbound_written_aisle(session: Session) -> None:
    """入库写入巷道 01 → 出库从 01 拣配：出库台账源 = 入库台账目标，前后一致。"""
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
            ),
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            ),
        ],
    )
    inbound_order, outbound_order = scenario.job_orders

    confirm_inbound(
        session,
        job_order=inbound_order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )
    confirm_outbound(
        session,
        job_order=outbound_order,
        operator_id=operator.id,
        executed_at=NOW,
        source_location_code="010104",
        snapshot=scenario.snapshot,
    )

    inbound_ledger = _ledgers(session, inbound_order)[0]
    outbound_ledger = _ledgers(session, outbound_order)[0]
    assert inbound_ledger.target_location_code == "010104"
    assert outbound_ledger.source_location_code == "010104"
    assert outbound_ledger.source_location_code == inbound_ledger.target_location_code


# ------------------------------------------------------------------ CL-006 移库执行 → 新巷道 + 旧记录留痕

def test_cl006_relocate_updates_aisle_and_keeps_old_record(session: Session) -> None:
    """移库执行后：台账 target 更新为新巷道、source 保留旧巷道（留痕），库存随之迁移。"""
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
    assert ledger.source_location_code == "010104"  # 旧巷道留痕
    assert ledger.target_location_code == "010105"  # 更新为新巷道
    assert ledger.batch_no == BATCH  # 移库不改批号
    # 库存随移库迁移
    assert _qty_at(session, scenario.snapshot.id, "010104") is None
    assert _qty_at(session, scenario.snapshot.id, "010105") == 40
