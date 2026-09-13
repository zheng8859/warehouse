"""入库管线（8 步）：导入 PO → 筛选 → 勾选 → 批量分配 → 逐单处置 → 批量确认 L1 → 写入库台账 → 落位后验。15 §2.1。

本模块落其中「批量确认 → 写入库台账」这一段的确认编排（design.md D5）：
`confirm_inbound` 把一张 `PLANNED` 的入库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写入库台账
（无源、有目标）与 cap 增量。后验（`EXECUTED → VERIFYING → VERIFIED/VERIFY_FAILED`）在
`verify.py`，由确认编排在台账写成功后同步接上 —— 见 `app/services/verify.py`。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.models.job import JobOrder
from app.models.linkage import Snapshot
from app.services.confirm import _confirm_and_execute
from app.services.verify import run_verification


def confirm_inbound(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    target_location_code: str,
    actual_qty: int | None = None,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """入库确认→执行→后验：目标库位落位，写入库台账 + cap 增量，迁 `EXECUTED` 再同步后验。

    返回同一个 `job_order`（`VERIFIED` / `VERIFY_FAILED`，或失败回退的 `PLANNED`）。
    台账矩阵：入库无源库位、有目标库位（15 附录A），由 `_LEDGER_LOCATION_CHECK` 钉在 DB 层。
    后验在台账写成功后同请求接上（`design.md` D2），`VERIFYING` 不对外停留。

    `lock_version` 透传给确认编排的乐观锁（见 `confirm._confirm_and_execute`）。
    """
    result = _confirm_and_execute(
        session,
        job_order=job_order,
        operator_id=operator_id,
        executed_at=executed_at,
        target_location_code=target_location_code,
        actual_qty=actual_qty,
        snapshot=snapshot,
        lock_version=lock_version,
        plan_json=plan_json,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )
    if result.status is JobStatus.EXECUTED:
        run_verification(session, job_order=result, snapshot=snapshot)
    return result
