"""出库管线（7 步）：导入 DO → 筛选 → 勾选 → 批量生成顺路取 → 逐单处置 → 批量确认 → 写出库台账 + 后验。15 §2.2。只读派生：不触发评分引擎、不重新决定落位。

本模块落「批量确认 → 写出库台账」这一段的确认编排（design.md D5）：
`confirm_outbound` 把一张 `PLANNED` 的出库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写出库台账
（有源、无目标）与 cap 增量（源库位 ↓）。出库是只读派生 —— 落位来自顺路取方案，本编排
**不重新决定落位**，只把方案里的源库位固化进台账。后验在 `verify.py`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.models.job import JobOrder
from app.models.linkage import Snapshot
from app.services.confirm import _confirm_and_execute
from app.services.verify import run_verification


def confirm_outbound(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    source_location_code: str,
    actual_qty: int | None = None,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    pick_path_json: list | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """出库确认→执行→后验：源库位出库，写出库台账 + cap 增量，迁 `EXECUTED` 再同步后验。

    返回同一个 `job_order`（`VERIFIED` / `VERIFY_FAILED`，或失败回退的 `PLANNED`）。
    台账矩阵：出库有源库位、无目标库位（15 附录A）。`pick_path_json` 记顺路取的拣货路径
    （巷道序）。后验的加权集中度由编排从台账行取拣货分布（v1 逐单粒度）。

    `lock_version` 透传给确认编排的乐观锁（见 `confirm._confirm_and_execute`）。
    """
    result = _confirm_and_execute(
        session,
        job_order=job_order,
        operator_id=operator_id,
        executed_at=executed_at,
        source_location_code=source_location_code,
        actual_qty=actual_qty,
        snapshot=snapshot,
        lock_version=lock_version,
        pick_path_json=pick_path_json,
        plan_json=plan_json,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )
    if result.status is JobStatus.EXECUTED:
        run_verification(session, job_order=result)
    return result
