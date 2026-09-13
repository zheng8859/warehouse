"""cap 事务增量（`apply_increment`）的三类口径与同事务契约测试（tasks.md 3.2 的验证）。

事实来源：16-数据衔接与 cap 自维护设计 §6.3（台账是 cap 增量唯一来源）
          17-数据模型设计 §3.3（InventoryItem 库存分布）、§3.4（AisleCap）
          openspec/changes/transaction-base/design.md D4（复用 to_occupied_cells 恒等占位）、
            D5（cap 增量与台账同事务）
          spec `transaction-base`「台账与 cap 增量同事务」

三条要钉住的口径：

  1. **三类增量方向**：入库目标 `+cells`、出库源 `−cells`、移库源 `−cells` 目标 `+cells`；
     反向行（`is_reversal`）方向取反 —— 冲正的「释放 cap / 减库存」正是这里的负号。
  2. **物质化载体是 `InventoryItem`，不是 `AisleCap`**（16 §6.3 / D4）：`cap_total =
     总格数 − 已占格数` 是导出值，由 4b 的全量重算在快照层固化。断言只盯库存分布，
     不碰 `AisleCap` 三列。
  3. **与台账同事务**：`apply_increment` 只 flush 不 commit；整体回滚时台账与库存增量
     一起消失（design.md D7 用真实事务而非应用补偿）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cap.increment import apply_increment
from app.core.enums import AccountStatus, JobType, Role
from app.core.errors import ValidationBlocked
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.models.linkage import InventoryItem
from app.services.ledger import write_ledger

from .conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"
MATERIAL = "M1"


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


def _qty_at(session: Session, snapshot_id: int, location_code: str) -> int | None:
    """取某库位在当前快照下的库存量；行不存在返回 `None`（区别于 `0`）。"""
    item = session.scalars(
        select(InventoryItem).where(
            InventoryItem.snapshot_id == snapshot_id,
            InventoryItem.location_code == location_code,
            InventoryItem.batch_no == BATCH,
            InventoryItem.material_code == MATERIAL,
        )
    ).first()
    return None if item is None else item.qty


def _ledger_count(session: Session, job_order: JobOrder) -> int:
    return len(
        session.scalars(select(Ledger).where(Ledger.job_order_id == job_order.id)).all()
    )


def _apply(session: Session, *, order: JobOrder, operator: Account, snapshot, source=None, target=None, is_reversal: bool = False) -> Ledger:
    ledger = write_ledger(
        session,
        job_order=order,
        source_location_code=source,
        target_location_code=target,
        operator_id=operator.id,
        executed_at=NOW,
        is_reversal=is_reversal,
    )
    apply_increment(session, ledger=ledger, snapshot=snapshot)
    return ledger


# ------------------------------------------------------------------ 三类增量口径

def test_inbound_increment_creates_inventory_at_target(session: Session) -> None:
    """入库：目标库位 `+cells`。库位原本没有该批号/料号 → 新建库存行。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.INBOUND)
        ],
    )
    _apply(session, order=scenario.job_orders[0], operator=operator, snapshot=scenario.snapshot, target="010104")

    assert _qty_at(session, scenario.snapshot.id, "010104") == 40


def test_inbound_increment_accumulates_existing(session: Session) -> None:
    """入库落在已有同批号/料号的库位 → 累加，而不是覆盖成一条新行。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=10)],
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.INBOUND)
        ],
    )
    _apply(session, order=scenario.job_orders[0], operator=operator, snapshot=scenario.snapshot, target="010104")

    assert _qty_at(session, scenario.snapshot.id, "010104") == 50


def test_outbound_increment_decrements_source(session: Session) -> None:
    """出库：源库位 `−cells`。减后仍有剩余 → 行保留、数量递减。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=50)],
        job_orders=[
            JobOrderSpec(order_no="DO-88", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.OUTBOUND)
        ],
    )
    _apply(session, order=scenario.job_orders[0], operator=operator, snapshot=scenario.snapshot, source="010104")

    assert _qty_at(session, scenario.snapshot.id, "010104") == 10


def test_outbound_increment_deletes_row_at_zero(session: Session) -> None:
    """出库减到 0 → 删行（`qty > 0` 是 InventoryItem 的 CHECK，留一行 0 会撞约束）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40)],
        job_orders=[
            JobOrderSpec(order_no="DO-88", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.OUTBOUND)
        ],
    )
    _apply(session, order=scenario.job_orders[0], operator=operator, snapshot=scenario.snapshot, source="010104")

    assert _qty_at(session, scenario.snapshot.id, "010104") is None


def test_relocate_increment_moves_source_to_target(session: Session) -> None:
    """移库：源库位 `−cells`、目标库位 `+cells`。批号不变（只调巷道，CLAUDE.md §四）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40)],
        job_orders=[
            JobOrderSpec(order_no="MV-01", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.RELOCATE)
        ],
    )
    _apply(session, order=scenario.job_orders[0], operator=operator, snapshot=scenario.snapshot, source="010104", target="010105")

    assert _qty_at(session, scenario.snapshot.id, "010104") is None
    assert _qty_at(session, scenario.snapshot.id, "010105") == 40


def test_reversal_inbound_releases_target(session: Session) -> None:
    """反向行方向取反：正常入库 `+cells`，反向入库 `−cells` —— 冲正「释放 cap / 减库存」。

    正向与反向两条台账行共存（`uq_ledgers_job_order_id_reversal`），净库存回到基线。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40)],
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.INBOUND)
        ],
    )
    order = scenario.job_orders[0]
    _apply(session, order=order, operator=operator, snapshot=scenario.snapshot, target="010104")
    assert _qty_at(session, scenario.snapshot.id, "010104") == 80

    _apply(session, order=order, operator=operator, snapshot=scenario.snapshot, target="010104", is_reversal=True)
    assert _qty_at(session, scenario.snapshot.id, "010104") == 40


# ------------------------------------------------------------------ 与台账同事务

def test_increment_and_ledger_roll_back_together(session: Session) -> None:
    """spec「台账与 cap 增量原子提交」：一条回滚同时撤掉台账与库存增量。

    先 `commit()` 固化基线（库存 qty=50），再写台账 + 增量，回滚后二者一起消失、
    基线不动 —— 证明增量没有越过事务边界自行提交（design.md D7 用真实事务而非应用补偿）。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=50)],
        job_orders=[
            JobOrderSpec(order_no="DO-88", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.OUTBOUND)
        ],
    )
    session.commit()  # 基线：库存 qty=50 已固化（保存点提交，见 tests/conftest.py）

    order = scenario.job_orders[0]
    _apply(session, order=order, operator=operator, snapshot=scenario.snapshot, source="010104")
    # 同一事务内：台账已落、库存已减（50 → 10）。
    assert _ledger_count(session, order) == 1
    assert _qty_at(session, scenario.snapshot.id, "010104") == 10

    session.rollback()
    # 整体回滚：台账与库存增量一起消失，基线原样 —— 原子性成立。
    assert _ledger_count(session, order) == 0
    assert _qty_at(session, scenario.snapshot.id, "010104") == 50


# ------------------------------------------------------------------ 边界

def test_outbound_increment_underflow_is_blocked(session: Session) -> None:
    """出库减穿现有量 → `ValidationBlocked`（负库存是数据自相矛盾，不能静默削掉）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=10)],
        job_orders=[
            JobOrderSpec(order_no="DO-88", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.OUTBOUND)
        ],
    )
    order = scenario.job_orders[0]

    with pytest.raises(ValidationBlocked):
        _apply(session, order=order, operator=operator, snapshot=scenario.snapshot, source="010104")


def test_increment_without_snapshot_is_a_noop(session: Session) -> None:
    """无当前快照 → 不物质化，台账仍作为唯一来源落库。

    快照缺失时没有可挂靠的库存明细，增量待下次快照导入后的全量重算 / 对账补齐
    （increment.py docstring）—— 这不是「静默丢弃」，台账行本身就是那段增量的事实记录。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        snapshot_time=None,  # 无快照形态（tasks.md 9.5）
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code=MATERIAL, qty=40, batch_no=BATCH, job_type=JobType.INBOUND)
        ],
    )
    order = scenario.job_orders[0]

    ledger = _apply(session, order=order, operator=operator, snapshot=None, target="010104")

    assert ledger.id is not None
    assert _ledger_count(session, order) == 1
