"""台账唯一写入入口的契约测试（tasks.md 3.1 的验证）。

事实来源：15-入库出库移库与后验流程设计 附录A（三类台账字段矩阵）
          openspec/changes/transaction-base/design.md D3（反向行）
          spec `transaction-base`「台账与 cap 增量同事务」场景「台账是增量的唯一来源」

三条要钉住的口径：

  1. **正常行 / 反向行共存的唯一键**：`uq_ledgers_job_order_id_reversal(job_order_id,
     is_reversal)` —— 一单至多一正常行 + 一反向行；第二条正常行被拒（「EXECUTED 后
    不允许重复写台账」的结构层兜底），一正常 + 一反向可共存。
  2. **字段矩阵**：入库无源库位、出库无目标库位、移库两者都有 —— 由
     `_LEDGER_LOCATION_CHECK` 钉在 DB 层，`write_ledger` 不按 `job_type` 反推。
  3. **写入即固化**：`write_ledger` 只 `add` + `flush`，不 commit —— 事务边界属于编排层，
     flush 让 CHECK / 唯一约束在事务内当场生效（「写台账失败」据此整体回滚）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobType, LedgerType, Role
from app.core.errors import ValidationBlocked
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.services.ledger import write_ledger

from .conftest import JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)


def _operator(session: Session) -> Account:
    """台账的 `operator_id` 是 NOT NULL 真外键，先造一个账号。"""
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


def _order(session: Session, *, job_type: JobType = JobType.INBOUND, **overrides) -> JobOrder:
    """一张带批号的作业单（台账的 batch_no 是 NOT NULL）。"""
    fields = dict(
        order_no="PO-01",
        material_code="M1",
        material_name="PET500 茉莉柚茶",
        qty=40,
        batch_no="GJP2571221",
        job_type=job_type,
    )
    fields.update(overrides)
    scenario = make_scenario(session, job_orders=[JobOrderSpec(**fields)])
    return scenario.job_orders[0]


def _count(session: Session, job_order: JobOrder) -> int:
    return len(
        session.scalars(select(Ledger).where(Ledger.job_order_id == job_order.id)).all()
    )


# ------------------------------------------------------------------ 正常行 / 反向行

def test_inbound_ledger_has_target_only(session: Session) -> None:
    """入库台账：无源库位、有目标库位（15 附录A 的矩阵）。"""
    operator = _operator(session)
    order = _order(session)

    ledger = write_ledger(
        session,
        job_order=order,
        target_location_code="010104",
        operator_id=operator.id,
        executed_at=NOW,
    )

    assert ledger.ledger_type is LedgerType.INBOUND
    assert ledger.source_location_code is None
    assert ledger.target_location_code == "010104"
    assert ledger.is_reversal is False
    assert ledger.qty == 40
    assert ledger.batch_no == "GJP2571221"


def test_outbound_ledger_has_source_only(session: Session) -> None:
    """出库台账：有源库位、无目标库位。"""
    operator = _operator(session)
    order = _order(session, job_type=JobType.OUTBOUND, order_no="DO-88")

    ledger = write_ledger(
        session,
        job_order=order,
        source_location_code="020301",
        operator_id=operator.id,
        executed_at=NOW,
    )

    assert ledger.ledger_type is LedgerType.OUTBOUND
    assert ledger.source_location_code == "020301"
    assert ledger.target_location_code is None


def test_relocate_ledger_has_both(session: Session) -> None:
    """移库台账：源库位与目标库位都有。"""
    operator = _operator(session)
    order = _order(session, job_type=JobType.RELOCATE, order_no="MV-01")

    ledger = write_ledger(
        session,
        job_order=order,
        source_location_code="010104",
        target_location_code="010105",
        operator_id=operator.id,
        executed_at=NOW,
    )

    assert ledger.ledger_type is LedgerType.RELOCATE
    assert ledger.source_location_code == "010104"
    assert ledger.target_location_code == "010105"


def test_normal_and_reversal_rows_coexist(session: Session) -> None:
    """一单至多一正常行 + 一反向行：第二条正常行被拒，一正常 + 一反向可共存。

    这是 `uq_ledgers_job_order_id_reversal(job_order_id, is_reversal)` 的直接语义
    （design.md D3 / 模型 docstring 第 12 条）。
    """
    operator = _operator(session)
    order = _order(session)

    normal = write_ledger(
        session,
        job_order=order,
        target_location_code="010104",
        operator_id=operator.id,
        executed_at=NOW,
    )
    reversal = write_ledger(
        session,
        job_order=order,
        target_location_code="010104",
        operator_id=operator.id,
        executed_at=NOW,
        is_reversal=True,
    )

    assert normal.is_reversal is False
    assert reversal.is_reversal is True
    assert _count(session, order) == 2


def test_second_normal_row_is_rejected(session: Session) -> None:
    """第二条正常行被唯一约束拒绝 —— 「EXECUTED 后不允许重复写台账」的结构层兜底。"""
    operator = _operator(session)
    order = _order(session)

    write_ledger(
        session,
        job_order=order,
        target_location_code="010104",
        operator_id=operator.id,
        executed_at=NOW,
    )
    with pytest.raises(IntegrityError):
        write_ledger(
            session,
            job_order=order,
            target_location_code="010105",
            operator_id=operator.id,
            executed_at=NOW,
        )
    session.rollback()


# ------------------------------------------------------------------ 字段口径与 DB CHECK

def test_inbound_with_source_is_rejected_by_check(session: Session) -> None:
    """入库台账冒出源库位 → `_LEDGER_LOCATION_CHECK` 拒绝。

    这正是 CHECK 存在的理由：把「入库无源库位」钉在 DB 层，而不是靠服务层自觉 ——
    在按巷道汇总 cap 增量时（16 §6.3），一个多余的源库位会被当成一次出库静默多减一格。
    """
    operator = _operator(session)
    order = _order(session)

    with pytest.raises(IntegrityError) as excinfo:
        write_ledger(
            session,
            job_order=order,
            source_location_code="010104",
            target_location_code="010105",
            operator_id=operator.id,
            executed_at=NOW,
        )
    assert "location_columns_by_type" in str(excinfo.value)
    session.rollback()


def test_outbound_without_source_is_now_allowed(session: Session) -> None:
    """出库台账 source 可空（D7）：拣货路径走 `pick_path_json`，源库位不再必填。

    旧 CHECK 的出库析取项 `source IS NOT NULL AND target IS NULL` 会把「无源出库」拒掉；
    放宽为 `target IS NULL` 后，source=None 转合法。台账矩阵（15 附录A）仍钉「出库无
    目标」，故 target 也留空 —— 拣货分布记在 `pick_path_json`（巷道粒度，17 §10.2）。
    """
    operator = _operator(session)
    order = _order(session, job_type=JobType.OUTBOUND, order_no="DO-88")

    ledger = write_ledger(
        session,
        job_order=order,
        pick_path_json=[{"aisle": "02", "qty": 40, "batches": ["GJP2571221"]}],
        operator_id=operator.id,
        executed_at=NOW,
    )

    assert ledger.ledger_type is LedgerType.OUTBOUND
    assert ledger.source_location_code is None
    assert ledger.target_location_code is None
    assert ledger.pick_path_json == [{"aisle": "02", "qty": 40, "batches": ["GJP2571221"]}]


def test_ledger_without_batch_no_is_rejected_before_write(session: Session) -> None:
    """没有生产批号的单不能写台账 —— 写入前拦成 `ValidationBlocked`，而不是让 flush
    抛一条难读的 NOT NULL 错误。"""
    operator = _operator(session)
    order = _order(session, batch_no=None)

    with pytest.raises(ValidationBlocked):
        write_ledger(
            session,
            job_order=order,
            target_location_code="010104",
            operator_id=operator.id,
            executed_at=NOW,
        )
    assert _count(session, order) == 0


def test_write_ledger_does_not_commit(session: Session) -> None:
    """`write_ledger` 只 flush 不 commit —— 事务边界属于编排层，这样「台账与 cap 同事务」
    才可能成立（design.md D7 用事务回滚而非应用补偿）。"""
    operator = _operator(session)
    order = _order(session)

    write_ledger(
        session,
        job_order=order,
        target_location_code="010104",
        operator_id=operator.id,
        executed_at=NOW,
    )
    # 本会话内可见（已 flush）
    assert _count(session, order) == 1
    # 整体回滚后不留任何行 —— 证明它没越过事务边界自行提交。
    session.rollback()
    assert _count(session, order) == 0
