"""移库管线（7 步）：KPI 识别偏离 → 筛选 → 勾选 → 批量生成收拢方案 → 逐单处置 → 批量确认执行 → 写移库台账 + 后验刷新。15 §2.3。目标巷道 = 主巷道；不改批号。

本模块落「批量确认执行 → 写移库台账」这一段的确认编排（design.md D5）：
`confirm_relocate` 把一张 `PLANNED` 的移库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写移库台账
（源 + 目标都有）与 cap 增量（源 ↓、目标 ↑）。**移库不改批号**（CLAUDE.md §四）—— 台账只记
源 / 目标库位，批号原样照抄。后验在 `verify.py`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.engine.factors import load_snapshot_index
from app.models.job import JobOrder
from app.models.linkage import Snapshot
from app.services.confirm import _confirm_and_execute
from app.services.verify import run_verification


def confirm_relocate(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    source_location_code: str,
    target_location_code: str,
    actual_qty: int | None = None,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """移库确认→执行→后验：源库位移到目标库位，写移库台账 + cap 增量，迁 `EXECUTED` 再同步后验。

    返回同一个 `job_order`（`VERIFIED` / `VERIFY_FAILED`，或失败回退的 `PLANNED`）。
    台账矩阵：移库源库位与目标库位都有（15 附录A）。

    移库后验是**相对阈值**（移库后跨巷道 < 移库前），故在增量**之前**先取「移库前」的同物料
    跨巷道数，交给后验编排与「移库后」比（`15-04` §8.1）。`lock_version` 透传给确认编排的
    乐观锁（见 `confirm._confirm_and_execute`）。
    """
    cross_aisle_before: int | None = None
    if snapshot is not None:
        pre = load_snapshot_index(session, snapshot_id=snapshot.id)
        cross_aisle_before = pre.profile(job_order.material_code).cross_aisle_count

    result = _confirm_and_execute(
        session,
        job_order=job_order,
        operator_id=operator_id,
        executed_at=executed_at,
        source_location_code=source_location_code,
        target_location_code=target_location_code,
        actual_qty=actual_qty,
        snapshot=snapshot,
        lock_version=lock_version,
        plan_json=plan_json,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )
    if result.status is JobStatus.EXECUTED:
        run_verification(
            session,
            job_order=result,
            snapshot=snapshot,
            cross_aisle_before=cross_aisle_before,
        )
    return result
