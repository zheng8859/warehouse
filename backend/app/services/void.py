"""冲正编排：整单冲掉，状态置 `VOID`，写反向台账行，同事务释放 cap / 减库存。

事实来源：15-入库出库移库与后验流程设计 §6.3（红冲 / 冲正，本实现有意偏离为简化）
          17-数据模型设计 §4.3（`Ledger.is_reversal` 反向行）
          openspec/changes/transaction-base/design.md D3（冲正 v1 简化）
          spec `transaction-base`「冲正回冲」

## 冲正做什么（D3 定稿）

冲正 = 原单 `EXECUTED / VERIFIED → VOID` + 写一条 `is_reversal=True` 的反向台账行 +
同事务反向 cap / 库存增量。**不创建反向 `JobOrder`**、不改写 / 删除历史台账（`ledgers`
没有 `updated_at`、没有删除接口）。重新入库按标准流程：导入新订单 → 推荐 → 确认。

反向行与正常行同落 `ledgers` 表、同字段、同口径，区别仅在 `is_reversal` ——
`apply_increment` 对反向行自动取反（`sign = -1`），于是入库目标库位 −、出库源库位 +、
移库源 + 目标 −，正是「释放 cap / 减少库存」。台账仍是 cap 与库存分布增量的唯一来源。

## 事务结构与守卫

与 `confirm._confirm_and_execute` 同一手法：守卫（`assert_transition(当前, VOID)`）在
savepoint **之外** —— 初始状态不是 `EXECUTED / VERIFIED`（已冲正 / 未执行）时抛
`StateConflict` 原样上抛（tasks 5.2 的守卫，二次确认已在端点 / 前端通过）。真正的三件套
（反向台账 → 反向增量 → `VOID` 迁移）包在 `begin_nested()` 里，任一步失败整体回滚、
单子留在原状态、错误上抛 —— 冲正要么全成、要么全不成，不存在「VOID 了却没回冲」的
半态（那会破坏「台账是 cap 增量唯一来源」的不变量，CLAUDE.md §四）。
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.cap.increment import apply_increment
from app.core.enums import JobStatus
from app.core.errors import ValidationBlocked
from app.core.state_machine import assert_transition
from app.models.job import JobOrder, Ledger
from app.models.linkage import Snapshot
from app.services.ledger import write_ledger


def _normal_ledger(session: Session, job_order: JobOrder) -> Ledger | None:
    """取该单的正常台账行（`is_reversal=False`）—— 冲正取反的对象。"""
    return session.scalars(
        sa.select(Ledger).where(
            Ledger.job_order_id == job_order.id, Ledger.is_reversal.is_(False)
        )
    ).first()


def void_job(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    voided_at: datetime,
    snapshot: Snapshot | None = None,
) -> JobOrder:
    """冲正：`EXECUTED / VERIFIED → VOID` + 反向台账行 + 反向 cap / 库存增量（同一事务）。

    返回**同一个** `job_order`（`VOID`）。不 `commit`：事务边界属于端点。冲正失败整体
    回滚，单子留在 `EXECUTED / VERIFIED`（错误上抛，端点据此拒绝冲正）。
    """
    # 1. 守卫（tasks 5.2）：仅 EXECUTED / VERIFIED 可冲正，VOID 后不可再冲正。
    #    只校验、不落库 —— 真正的 VOID 迁移在 savepoint 内与反向台账同进退。
    assert_transition(job_order.status, JobStatus.VOID)

    normal = _normal_ledger(session, job_order)
    if normal is None:
        raise ValidationBlocked(
            f"作业单 #{job_order.id} 无正常台账行 —— 冲正需原台账来取反，"
            "台账缺失属数据自相矛盾，无从回冲",
            detail={"job_order_id": job_order.id},
        )

    # 2. 冲正三件套：反向台账 → 反向增量 → VOID，同一 savepoint，任一步失败整体回滚。
    try:
        with session.begin_nested():
            reverse = write_ledger(
                session,
                job_order=job_order,
                source_location_code=normal.source_location_code,
                target_location_code=normal.target_location_code,
                operator_id=operator_id,
                executed_at=voided_at,
                # 反向行照抄原行的拣货路径 —— 出库按 `pick_path_json` 逐巷扣减（D7），
                # 冲正回补也必须按同一份巷道级路径取反，否则反向行 source/pick_path 双空，
                # `apply_increment` 无从回补。
                pick_path_json=normal.pick_path_json,
                is_reversal=True,
            )
            apply_increment(session, ledger=reverse, snapshot=snapshot)
            job_order.status = assert_transition(job_order.status, JobStatus.VOID)
            session.flush()
    except Exception:
        # savepoint 已回滚（反向台账、反向增量、VOID 迁移都没了）；内存态可能残留 VOID，
        # 先 expire 回库再上抛 —— 端点据此整体回滚，单子留在 EXECUTED / VERIFIED。
        session.expire(job_order)
        raise
    return job_order
