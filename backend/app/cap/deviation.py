"""全量偏离扫描：18 §1.5「KPI 识别偏离」的「导入」触发路径。

事实来源：18-KPI 设计 §1.5（事件触发 → 阈值比对「标记偏离批次」）
          17-数据模型设计 §4.4（Deviation 实体 / 物料级标识 / 成因分类）
          15-04 移库设计 §4.1（偏离成因：新入库收拢不达标 vs 历史库存拖累）
          spec `transaction-base`「偏离批次标记」

## 与 `verify._write_deviations` 的分工（两条触发路径）

`verify._write_deviations`（services 层）是「后验完成」路径：一张作业单后验超标，就落
一条 `Deviation`（标识 = 物料 + 批号）。本模块是「导入」路径：一份 INV 快照建完基线，
把**同物料跨巷道 > 阈值**的物料**全量**落成物料级 `Deviation`（批号留空）—— 这才是
P6 偏离批次表 / P5 移库任务来源该看到的完整偏离清单，而不是只有后验撞见的那几条。

## 口径

- **物料级**：偏离是物料级事实（`actual_cross_aisle` = 同物料跨巷道数），`batch_no` 留空
  （`identifier_required` 由 `material_code` 满足）。「按物料收拢散批」的移库按
  `material_code` 扇出散批，物料级偏离正是它的任务来源。
- **成因恒「历史库存拖累」**：全量扫描看的是存量分布，不是某次入库动作（`15-04` §4.1）。
- **跨巷道**：`location_code[:2]` 去重计数，与 `aggregate_aisle_caps` / `verify_inbound`
  同一切片口径（CLAUDE.md §七「库位号前 2 位 = 巷道」）。

## 幂等（只补缺）

某物料已有一条 `Deviation`（无论批号、状态）就跳过，不重复落 —— 重复导入 / 重复扫描
不会累积重复行；已「发起移库」/「已改善」的偏离同样跳过，不覆盖人工处置的进展。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job import Deviation, DeviationCauseKind, DeviationStatus
from app.models.linkage import InventoryItem

__all__ = ["DEFAULT_MATERIAL_CROSS_AISLE", "scan_material_deviations"]

#: 同物料跨巷道阈值（15-00 §六：同物料 ≤5）。与 `verify.DEFAULT_MATERIAL_CROSS_AISLE`
#: 同值 —— ① 层按层独立成常量，不 import ② 层 `services/verify`（保持层依赖方向）。
DEFAULT_MATERIAL_CROSS_AISLE = 5


def scan_material_deviations(
    session: Session,
    *,
    warehouse_id: str,
    snapshot_id: int,
    threshold: int = DEFAULT_MATERIAL_CROSS_AISLE,
) -> list[Deviation]:
    """扫一份快照，把「同物料跨巷道 > `threshold`」的物料落成物料级 `Deviation`。

    幂等：只补缺 —— 某物料已有一条 `Deviation`（无论批号、状态）即跳过。返回本次
    **新落**的 `Deviation` 列表（无缺口时为空），供调用方报告 / 断言。不 `commit`：
    事务边界属于调用方（与 `establish_baseline` 同一口径）。
    """
    items = session.scalars(
        select(InventoryItem).where(InventoryItem.snapshot_id == snapshot_id)
    ).all()

    aisles_by_material: dict[str, set[str]] = {}
    for item in items:
        aisles_by_material.setdefault(item.material_code, set()).add(item.location_code[:2])

    # 确定性：同样输入必得同样输出（CLAUDE.md §四），按物料号升序产出。
    deviating = sorted(
        (
            (material_code, len(aisles))
            for material_code, aisles in aisles_by_material.items()
            if len(aisles) > threshold
        ),
        key=lambda pair: pair[0],
    )
    if not deviating:
        return []

    codes = [material_code for material_code, _ in deviating]
    existing = set(
        session.scalars(
            select(Deviation.material_code).where(
                Deviation.warehouse_id == warehouse_id,
                Deviation.material_code.in_(codes),
            )
        )
    )

    created: list[Deviation] = []
    for material_code, aisle_count in deviating:
        if material_code in existing:
            continue
        deviation = Deviation(
            warehouse_id=warehouse_id,
            material_code=material_code,
            batch_no=None,
            actual_cross_aisle=aisle_count,
            threshold_cross_aisle=threshold,
            cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
        )
        session.add(deviation)
        created.append(deviation)
    session.flush()
    return created


def refresh_material_deviations(
    session: Session,
    *,
    warehouse_id: str,
    snapshot_id: int,
    threshold: int = DEFAULT_MATERIAL_CROSS_AISLE,
) -> list[Deviation]:
    """按最新快照库存刷新物料级偏离（「最新偏离批次表」），返回刷新后的全部物料级偏离。

    出库 / 移库都会改变库存分布，`Deviation.actual_cross_aisle` 若不随库存刷新，就会停在
    过账前的旧值（例：物料已被出库清空，偏离表仍显示 4 巷）。本函数用当前快照重算：
    - 新出现的散批物料（跨巷 > `threshold` 且无偏离行）→ 补一条 `OPEN`（复用
      `scan_material_deviations` 的「只补缺」）。
    - 既有物料级偏离 → `actual_cross_aisle` 刷新为当前巷数（物料已无库存则为 0）；
      状态按「当前是否仍散批」归位：散批 → `OPEN`、不再散批 → `IMPROVED`。
      「已发起移库」保持不动 —— 移库在途，其推进由 `verify._reconcile_relocate_deviation`
      负责，这里不覆盖人工处置进展。

    不 `commit`（事务边界归调用方，与 `establish_baseline` 同一口径）。
    """
    # 先补缺（新散批物料），再刷新全部既有物料级偏离。
    scan_material_deviations(
        session, warehouse_id=warehouse_id, snapshot_id=snapshot_id, threshold=threshold
    )

    items = session.scalars(
        select(InventoryItem).where(InventoryItem.snapshot_id == snapshot_id)
    ).all()
    aisles_by_material: dict[str, set[str]] = {}
    for item in items:
        aisles_by_material.setdefault(item.material_code, set()).add(item.location_code[:2])

    deviations = list(
        session.scalars(
            select(Deviation)
            .where(
                Deviation.warehouse_id == warehouse_id,
                Deviation.batch_no.is_(None),
            )
            .order_by(Deviation.id)
        )
    )
    for deviation in deviations:
        current = len(aisles_by_material.get(deviation.material_code, set()))
        deviation.actual_cross_aisle = current
        if deviation.status is not DeviationStatus.RELOCATE_STARTED:
            deviation.status = (
                DeviationStatus.OPEN if current > threshold else DeviationStatus.IMPROVED
            )
    session.flush()
    return deviations
