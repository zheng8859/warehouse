"""台账事务增量：入库 ↑已占、出库 ↓已占、移库源 ↓ 目标 ↑；与台账同事务写入。

事实来源：16-数据衔接与 cap 自维护设计 §6.3（台账是 cap 增量唯一来源）
          17-数据模型设计 §3.3（InventoryItem 库存分布）、§3.4（AisleCap）
          openspec/changes/transaction-base/design.md D4（复用 to_occupied_cells 恒等占位）、
            D5（cap 增量与台账同事务）
          spec `transaction-base`「台账与 cap 增量同事务」

## 关键口径：增量落在 InventoryItem，不落 AisleCap

`AisleCap` 是「快照时刻的冻结值」（`app/models/linkage.py` 的类 docstring 明写
「事务内增量不落本表」）—— `cap_total` 等三列在快照导入时全量重算，事务不该改写它。
本阶段 cap 增量的**物质化载体是 `InventoryItem`（库存分布）**：入库在目标库位增库存、
出库在源库位减库存、移库源减目标增，冲正反向。台账仍是「为什么库存变了」的唯一来源。

「已占格数」随库存分布增减而隐式变化（`cap_total = 总格数 − 已占格数` 是**导出值**，
由 4b 的全量重算 / 对账在快照层固化），本函数不写 `AisleCap` 三列。

## 板-格换算的单一来源

`to_occupied_cells(qty)`（`app/engine/allocator.py`）是「占几格」的唯一落点（D6），
本函数不另写换算 —— 分配侧与增量侧必须用同一口径，否则「台账是 cap 增量唯一来源」
会被两套换算偷偷打破（design.md D4 / 风险表）。
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import ItemStatus, LedgerType
from app.core.errors import ValidationBlocked
from app.engine.allocator import to_occupied_cells
from app.models.job import Ledger
from app.models.linkage import InventoryItem, Snapshot

#: 新建库存行时的品质状态默认值：源数据（PO / 入库）只有「合格」能落到新占用上，
#: 待检 / 冻结属品管流程（不归本系统，`ItemStatus` 的类 docstring）。
_DEFAULT_ITEM_STATUS = ItemStatus.QUALIFIED.value


def _find_item(
    session: Session,
    *,
    snapshot_id: int,
    location_code: str,
    batch_no: str,
    material_code: str,
) -> InventoryItem | None:
    """按库存行的业务唯一键（快照 + 库位 + 批号 + 料号）取既有行，没有返回 None。"""
    return session.scalars(
        sa.select(InventoryItem).where(
            InventoryItem.snapshot_id == snapshot_id,
            InventoryItem.location_code == location_code,
            InventoryItem.batch_no == batch_no,
            InventoryItem.material_code == material_code,
        )
    ).first()


def _mutate(
    session: Session,
    *,
    snapshot: Snapshot,
    location_code: str,
    batch_no: str,
    material_code: str,
    material_name: str | None,
    delta: int,
) -> None:
    """在 `location_code` 上对库存数量施加 `delta`（正增负减），落到当前快照的明细行。

    - `delta > 0`：有行则累加，无行则新建（`item_status` 用默认「合格」，`snapshot_time`
      取本快照时点 —— 事务内新增的库存行仍属于这份快照的「活」明细，4b 的全量重算会
      在下一版快照把它归一）。
    - `delta < 0`：有行则递减；减到 0 删行（`qty > 0` 是 `InventoryItem` 的 CHECK，留一行
      `qty = 0` 会撞约束）；减穿（> 现有量）是数据自相矛盾，显式拦成 `ValidationBlocked`。
    """
    item = _find_item(
        session,
        snapshot_id=snapshot.id,
        location_code=location_code,
        batch_no=batch_no,
        material_code=material_code,
    )
    if delta > 0:
        if item is None:
            session.add(
                InventoryItem(
                    warehouse_id=snapshot.warehouse_id,
                    snapshot_id=snapshot.id,
                    location_code=location_code,
                    material_code=material_code,
                    material_name=material_name,
                    batch_no=batch_no,
                    item_status=_DEFAULT_ITEM_STATUS,
                    qty=delta,
                    snapshot_time=snapshot.snapshot_time,
                )
            )
        else:
            item.qty += delta
    elif delta < 0:
        if item is None:
            raise ValidationBlocked(
                f"库存分布中不存在可扣减的行（快照 #{snapshot.id} / 库位 {location_code} / "
                f"批号 {batch_no} / 料号 {material_code}）—— 增量与快照对不上，"
                "台账仍在但库存无法回补，属数据自相矛盾",
                detail={
                    "snapshot_id": snapshot.id,
                    "location_code": location_code,
                    "batch_no": batch_no,
                    "material_code": material_code,
                },
            )
        remaining = item.qty + delta  # delta 为负
        if remaining < 0:
            raise ValidationBlocked(
                f"库位 {location_code} 的 {material_code}/{batch_no} 现有 {item.qty}，"
                f"不足以扣减 {abs(delta)} —— 增量会导致负库存",
                detail={
                    "location_code": location_code,
                    "material_code": material_code,
                    "batch_no": batch_no,
                    "existing_qty": item.qty,
                    "delta": delta,
                },
            )
        if remaining == 0:
            session.delete(item)
        else:
            item.qty = remaining


def apply_increment(session: Session, *, ledger: Ledger, snapshot: Snapshot | None) -> None:
    """把一条台账行的 cap / 库存分布增量落到 `InventoryItem`，与台账同事务。

    三类口径（`is_reversal` 反向行时方向取反，`sign = -1`）：

    - 入库（INBOUND）：目标库位 `+cells`
    - 出库（OUTBOUND）：源库位 `-cells`
    - 移库（RELOCATE）：源库位 `-cells`、目标库位 `+cells`

    `cells = to_occupied_cells(ledger.qty)`（板-格换算单一来源，本阶段恒等）。

    `snapshot` 为 `None`（无当前快照）时**不物质化**：没有快照就没有可挂靠的库存明细，
    台账仍作为唯一来源落库，增量待下次快照导入后的全量重算 / 对账补齐 —— 这不是
    「静默丢弃」，台账行本身就是那段增量的事实记录。
    """
    if snapshot is None:
        return

    cells = to_occupied_cells(ledger.qty)
    sign = -1 if ledger.is_reversal else 1

    source = ledger.source_location_code
    target = ledger.target_location_code

    if ledger.ledger_type is LedgerType.INBOUND:
        assert target is not None  # `_LEDGER_LOCATION_CHECK` 已保证
        _mutate(
            session,
            snapshot=snapshot,
            location_code=target,
            batch_no=ledger.batch_no,
            material_code=ledger.material_code,
            material_name=ledger.material_name,
            delta=cells * sign,
        )
    elif ledger.ledger_type is LedgerType.OUTBOUND:
        assert source is not None
        _mutate(
            session,
            snapshot=snapshot,
            location_code=source,
            batch_no=ledger.batch_no,
            material_code=ledger.material_code,
            material_name=ledger.material_name,
            delta=-cells * sign,
        )
    else:  # RELOCATE
        assert source is not None and target is not None
        _mutate(
            session,
            snapshot=snapshot,
            location_code=source,
            batch_no=ledger.batch_no,
            material_code=ledger.material_code,
            material_name=ledger.material_name,
            delta=-cells * sign,
        )
        _mutate(
            session,
            snapshot=snapshot,
            location_code=target,
            batch_no=ledger.batch_no,
            material_code=ledger.material_code,
            material_name=ledger.material_name,
            delta=cells * sign,
        )

    session.flush()
