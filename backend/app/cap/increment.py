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
出库按拣货路径逐巷减库存（回退单源）、移库源减目标增，冲正反向。台账仍是「为什么库存
变了」的唯一来源。

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


def _aisle_items(
    session: Session,
    *,
    snapshot: Snapshot,
    aisle: str,
    material_code: str,
) -> list[InventoryItem]:
    """该巷道、该料号的库存行，按库位号升序（同库位按批号升序）—— 确定性分配。"""
    return list(
        session.scalars(
            sa.select(InventoryItem)
            .where(
                InventoryItem.snapshot_id == snapshot.id,
                InventoryItem.location_code.like(f"{aisle}%"),
                InventoryItem.material_code == material_code,
            )
            .order_by(InventoryItem.location_code, InventoryItem.batch_no)
        )
    )


def _mutate_aisle(
    session: Session,
    *,
    snapshot: Snapshot,
    aisle: str,
    material_code: str,
    delta: int,
) -> None:
    """在巷道 `aisle` 上对库存总量施加 `delta`（正增负减）。

    - `delta < 0`（出库扣减）：按库位号升序逐行递减至扣完；减到 0 删行；减穿（巷内总量
      不足）→ `ValidationBlocked`。批号不参与过滤 —— 拣货路径的 `batches` 是派生时刻该巷
      的批号全集，与巷内现状一致，按巷道总量扣即可（D7 粒度说明：消费方都按 `[:2]` 聚合）。
    - `delta > 0`（冲正回补）：加回到巷内首行（库位号升序）；巷内已无行（正向扣减删空）
      → `ValidationBlocked` —— 巷道级回补无从知道被删库位的确切位置，宁可响亮失败也不
      编造一个库位。
    """
    items = _aisle_items(
        session, snapshot=snapshot, aisle=aisle, material_code=material_code
    )
    if delta < 0:
        remaining = -delta
        for item in items:
            if remaining <= 0:
                break
            take = min(item.qty, remaining)
            item.qty -= take
            remaining -= take
        if remaining > 0:
            raise ValidationBlocked(
                f"巷道 {aisle} 的 {material_code} 现有总量不足以扣减 {-delta} —— "
                "拣货路径与库存视图对不上，增量会导致负库存",
                detail={"aisle": aisle, "material_code": material_code, "delta": delta},
            )
        for item in items:
            if item.qty == 0:
                session.delete(item)
    else:  # delta > 0
        if not items:
            raise ValidationBlocked(
                f"巷道 {aisle} 的 {material_code} 已无库存行，无法回补 {delta} —— "
                "正向扣减删空了该巷道，巷道级回补无从恢复逐库位明细",
                detail={"aisle": aisle, "material_code": material_code, "delta": delta},
            )
        items[0].qty += delta


def _apply_outbound(
    session: Session, *, ledger: Ledger, snapshot: Snapshot, sign: int
) -> None:
    """出库扣减：优先按 `pick_path_json` 逐巷扣减，回退 `source_location_code` 单巷。

    拣货路径是**巷道级**（`17` §10.2 的 `aisle`），`InventoryItem` 是**库位级** —— 扣减只要
    在巷道总量上正确（D7 粒度说明），故按巷道聚合扣，不引入库位级业务语义。两者皆无
    （既无拣货路径也无源库位）→ `ValidationBlocked`（不静默）。
    """
    if ledger.pick_path_json:
        for entry in ledger.pick_path_json:
            _mutate_aisle(
                session,
                snapshot=snapshot,
                aisle=entry["aisle"],
                material_code=ledger.material_code,
                delta=-to_occupied_cells(entry["qty"]) * sign,
            )
    elif ledger.source_location_code is not None:
        _mutate(
            session,
            snapshot=snapshot,
            location_code=ledger.source_location_code,
            batch_no=ledger.batch_no,
            material_code=ledger.material_code,
            material_name=ledger.material_name,
            delta=-to_occupied_cells(ledger.qty) * sign,
        )
    else:
        raise ValidationBlocked(
            f"出库台账 #{ledger.id} 既无拣货路径（pick_path_json）也无源库位 —— "
            "无法定位扣减，属数据自相矛盾",
            detail={"ledger_id": ledger.id},
        )


def apply_increment(session: Session, *, ledger: Ledger, snapshot: Snapshot | None) -> None:
    """把一条台账行的 cap / 库存分布增量落到 `InventoryItem`，与台账同事务。

    三类口径（`is_reversal` 反向行时方向取反，`sign = -1`）：

    - 入库（INBOUND）：目标库位 `+cells`
    - 出库（OUTBOUND）：按 `pick_path_json` 逐巷 `-cells`（回退 `source_location_code` 单巷）
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
        _apply_outbound(session, ledger=ledger, snapshot=snapshot, sign=sign)
    else:  # RELOCATE
        assert source is not None and target is not None
        source_locations = (ledger.plan_json or {}).get("source_locations")
        if source_locations:
            # 逐格源库位（缺口 2）：散落板跨多个库位，按 plan_json 里的真实库位号逐格扣减。
            # 台账行仍是单行（`source_location_code` 只是代表库位），完整搬出明细在这里。
            for entry in source_locations:
                _mutate(
                    session,
                    snapshot=snapshot,
                    location_code=entry["location_code"],
                    batch_no=ledger.batch_no,
                    material_code=ledger.material_code,
                    material_name=ledger.material_name,
                    delta=-to_occupied_cells(entry["qty"]) * sign,
                )
        else:
            # 兼容无逐格清单的历史行：回退单源库位扣减（本阶段之前的移库台账）。
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
