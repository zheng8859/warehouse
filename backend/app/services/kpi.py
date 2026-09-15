"""KPI 聚合：偏离清单（移库任务来源）+ 最小聚合（阶段六收编点）。

事实来源：15-入库出库移库与后验流程设计 §7.2（偏离批次治理回路）
          18-KPI 设计（加权集中度 / 采纳率 / 落位准确率 → KpiSnapshot）
          openspec/changes/transaction-base/design.md D5（偏离清单聚合）
          openspec/changes/ai-assist/design.md D1（最小聚合，作为 ①② 的 rule 输入）
          spec `transaction-base`「偏离批次标记」、spec `ai-assist`「① KPI 解读」

`list_deviations`：阶段四落的「偏离清单」读入口 —— 后验超标写入的 `Deviation` 行
（`verify._write_deviations`）是移库作业的任务来源（`Verification → Deviation → JobOrder`，
17 §4.4）。

`same_material_cross_aisle_mean` / `weighted_concentration` / `adoption_rate`：冷路径 ①
KPI 解读要的三个「规则算的数」，口径逐条对齐 18 §1.3。**阶段六收编点** —— 阶段六
`KpiSnapshot` 全量聚合（按周/月落库 + 环比）落地时，这三条纯函数的口径被收编进去，
此处不重复维护；本阶段只提供「够 ① 叙事用」的最小纯函数，不做快照落库。
"""
from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobType, LedgerType
from app.models.job import Deviation, DeviationStatus, JobOrder, Ledger
from app.models.linkage import InventoryItem, Snapshot


def list_deviations(
    session: Session,
    *,
    warehouse_id: str,
    status: DeviationStatus | None = None,
) -> list[Deviation]:
    """按仓列出偏离清单，`status` 可选过滤（默认全部），按 `created_at` 升序返回。

    「偏离清单」是移库作业的任务来源（15 §7.2）：操作员从这里挑偏离批次发起收拢。
    不做分页 / 聚合 —— 阶段六的 KPI 看板再补，本函数只落「可查」（design.md D5）。
    """
    stmt = sa.select(Deviation).where(Deviation.warehouse_id == warehouse_id)
    if status is not None:
        stmt = stmt.where(Deviation.status == status)
    stmt = stmt.order_by(Deviation.created_at, Deviation.id)
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# 最小聚合（阶段六收编点）—— 冷路径 ① KPI 解读的 rule 输入。口径 = 18 §1.3。
# ---------------------------------------------------------------------------


def same_material_cross_aisle_mean(items: Iterable[tuple[str, str]]) -> float:
    """同物料跨巷道均值（18 §1.3 L39）—— 全库所有物料的跨巷道数取均值，目标 ≤5。

    `items` = `(material_code, aisle_no)` 行（`aisle_no = location_code[:2]`，与
    `engine/factors.py` 的 `aisle_of` 同口径）。**分母是物料数不是库存行数**：同一料号
    的多行（多库位 / 多批）只数它占了几个巷道一次。空输入 → 0.0（不抛异常，STALE 由
    调用方标注，18 §9.1）。
    """
    aisles: dict[str, set[str]] = {}
    for material, aisle in items:
        aisles.setdefault(material, set()).add(aisle)
    if not aisles:
        return 0.0
    return sum(len(a) for a in aisles.values()) / len(aisles)


def weighted_concentration(
    aisle_quantities: Iterable[tuple[str, int]], *, threshold: float = 0.8
) -> int:
    """拣货量加权集中度（18 §1.3 L36）—— 单 DO 按拣货量降序累加至 `threshold` 所覆盖
    的巷道数 N（默认 80%、N 默认 ≤5）。统一验收指标。

    `aisle_quantities` = `(aisle_no, qty)` 行（一张 DO 的拣货量分布）。空单 / 零量 → 0。
    """
    rows = sorted(aisle_quantities, key=lambda item: item[1], reverse=True)
    total = sum(qty for _, qty in rows)
    if total <= 0:
        return 0
    target = total * threshold
    accrued = 0
    for n, (_, qty) in enumerate(rows, start=1):
        accrued += qty
        if accrued >= target:
            return n
    return len(rows)


def adoption_rate(adopted: int, total: int) -> float:
    """推荐采纳率（18 §1.3 L41）—— 接受/微调后确认 ÷ 总推荐，目标 ≥60%。

    `total == 0` → 0.0（零推荐不是 NaN，也不是 100% 的虚高）。
    """
    if total == 0:
        return 0.0
    return adopted / total


def compute_kpi_summary(session: Session, *, warehouse_id: str) -> dict:
    """KPI 看板四个指标的聚合 —— 全部从真实数据计算，不硬编码演示值（阶段六收编点）。

    口径（逐条对齐 `18` §1.3）：

    - `material_cross_aisle_mean`：同物料跨巷道均值 —— 当前快照库存里每个物料跨的巷道数
      取均值（`same_material_cross_aisle_mean`，目标 ≤5）。**从库存取值**。
    - `weighted_concentration`：拣货量加权集中度 —— 已出库台账（`Ledger.pick_path_json`）
      逐单 `weighted_concentration` 取均值（目标 ≤5）。
    - `per_do_cross_aisle_mean`：单张开单跨巷道均值 —— 已出库台账每单跨的巷道数取均值
      （参考监控，不作硬验收）。
    - `adoption_rate`：推荐采纳率 —— 入库落位推荐「已确认（VERIFIED）÷ 已出推荐
      （VERIFIED + PLANNED + REJECTED）」。

    无数据（无快照 / 无出库台账 / 无入库推荐）时对应项为 0.0，不抛异常。
    """
    snapshot = session.scalars(
        sa.select(Snapshot)
        .where(Snapshot.warehouse_id == warehouse_id)
        .order_by(Snapshot.version_no.desc())
        .limit(1)
    ).first()

    # 1. 同物料跨巷道均值 —— 从库存取值（material_code, location_code[:2]）。
    material_cross_aisle_mean = 0.0
    if snapshot is not None:
        pairs = session.execute(
            sa.select(InventoryItem.material_code, InventoryItem.location_code).where(
                InventoryItem.snapshot_id == snapshot.id
            )
        ).all()
        material_cross_aisle_mean = same_material_cross_aisle_mean(
            (material, location[:2]) for material, location in pairs
        )

    # 2/3. 出库台账拣货路径的加权集中度 / 单张开单跨巷道均值。
    outbound_ledgers = session.scalars(
        sa.select(Ledger).where(
            Ledger.warehouse_id == warehouse_id,
            Ledger.ledger_type == LedgerType.OUTBOUND,
            Ledger.is_reversal.is_(False),
            Ledger.pick_path_json.is_not(None),
        )
    ).all()
    concentrations: list[int] = []
    aisle_counts: list[int] = []
    for ledger in outbound_ledgers:
        pick = ledger.pick_path_json or []
        qty_by_aisle = {entry["aisle"]: int(entry.get("qty") or 0) for entry in pick}
        concentrations.append(weighted_concentration(qty_by_aisle.items()))
        aisle_counts.append(len(qty_by_aisle))
    weighted_concentration_mean = (
        sum(concentrations) / len(concentrations) if concentrations else 0.0
    )
    per_do_cross_aisle_mean = (
        sum(aisle_counts) / len(aisle_counts) if aisle_counts else 0.0
    )

    # 4. 推荐采纳率 —— 入库落位推荐：已确认 ÷ 已出推荐。
    adopted = 0
    total = 0
    for status, count in session.execute(
        sa.select(JobOrder.status, sa.func.count())
        .where(
            JobOrder.warehouse_id == warehouse_id,
            JobOrder.job_type == JobType.INBOUND,
            JobOrder.status.in_(
                [JobStatus.VERIFIED, JobStatus.PLANNED, JobStatus.REJECTED]
            ),
        )
        .group_by(JobOrder.status)
    ).all():
        total += count
        if status is JobStatus.VERIFIED:
            adopted = count
    adoption = adoption_rate(adopted, total)

    return {
        "material_cross_aisle_mean": round(material_cross_aisle_mean, 2),
        "weighted_concentration": round(weighted_concentration_mean, 2),
        "per_do_cross_aisle_mean": round(per_do_cross_aisle_mean, 2),
        "adoption_rate": round(adoption, 4),
    }
