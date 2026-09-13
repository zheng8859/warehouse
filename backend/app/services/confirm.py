"""三类作业的确认→执行编排链（**共用一条链，按 `job_type` 分派**，design.md D5）。

事实来源：15-入库出库移库与后验流程设计 §6.2/§6.3（逐单处置与写台账）、§3.1（状态机）
          17-数据模型设计 §4.1（作业单执行侧字段）、§4.5（逐单处置）
          openspec/changes/transaction-base/design.md D5（六模块 + 单事务）、D7（事务/幂等）
          spec `transaction-base`「三类作业确认与落位执行」

## 编排链

    [乐观锁] PLANNED ──确认──▶ CONFIRMED ──写台账 + cap 增量──▶ EXECUTED

第 1 步（确认）与第 2 步（执行）在**同一个 DB 事务**里：第 1 步把「决策权在人」的那一下
（确认人 / 时刻 / 实际落位）落库并 flush；第 2 步用 `session.begin_nested()`（savepoint）
包住「写台账 → cap 增量 → `CONFIRMED → EXECUTED`」，任一步失败 savepoint 整体回滚，
再走 `CONFIRMED → PLANNED` 回边把单子退回已出方案（确认作废，须重新确认）。

**为什么是 savepoint 而不是 `session.rollback()`**：`session.rollback()` 会连调用方
尚未提交的基线（作业单、快照、账号）一起滚掉；savepoint 只滚第 2 步自己，第 1 步的
`CONFIRMED` 与调用方的基线都留在外层事务里 —— 「台账与 cap 增量原子提交」因此仍由
真实事务兑现，而不是应用层补偿（CLAUDE.md §四 / design.md D7）。

**两道守卫（都在 try 之外，原样上抛）**：
1. **乐观锁** `bump_lock_version` —— 先提交者推进版本、后提交者被拒（spec `data-model`
   「并发确认仅一方成功」）。
2. **状态机** `assert_transition(PLANNED, CONFIRMED)` —— 初始状态不是 `PLANNED`
   （已确认过 / 已执行 / 已冲正）时抛 `StateConflict`。`EXECUTED` 后重复确认撞的
   正是这道守卫（15 §11.7），不会写出第二条台账。两道都是「调用方过期」，不是
   「执行失败」，不该被降级吞掉。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.cap.increment import apply_increment
from app.core.concurrency import bump_lock_version
from app.core.enums import JobStatus
from app.core.state_machine import assert_transition
from app.models.job import JobOrder
from app.models.linkage import Snapshot
from app.services.ledger import write_ledger


def _demote_to_planned(session: Session, job_order: JobOrder) -> None:
    """写台账 / cap 增量失败后，把单子从 `CONFIRMED` 退回 `PLANNED`（回边，确认作废）。

    保存点回滚后，`job_order` 的内存态可能残留 `EXECUTED`（`begin_nested` 只滚数据库、
    不滚 ORM 对象），故先 `expire` 让它回数据库读到 `CONFIRMED`，再走回边。确认侧的
    三处痕迹一并清空 —— 「未确认不产生台账」要查得下去，就不能让一张 `PLANNED` 的单
    还带着上一轮的确认人 / 实际落位。
    """
    session.expire(job_order)  # 内存态回数据库（savepoint 已回滚，DB 上是 CONFIRMED）
    job_order.status = assert_transition(job_order.status, JobStatus.PLANNED)
    job_order.confirmed_by_id = None
    job_order.confirmed_at = None
    job_order.actual_location_code = None
    job_order.actual_qty = None
    session.flush()


def _confirm_and_execute(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    source_location_code: str | None = None,
    target_location_code: str | None = None,
    actual_qty: int | None = None,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    pick_path_json: list | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """确认→执行的主编排：乐观锁 → 确认落位 → 写台账 + cap 增量 → 迁 `EXECUTED`。

    返回**同一个** `job_order`（成功时是 `EXECUTED`，失败时是 `PLANNED`）—— 调用方按
    `job_order.status` 区分，不必另收一份结果对象。执行失败被**就地降级**而非上抛，
    因为批量确认要「个别单失败回 `PLANNED` 不影响同批其余单」（spec「批量确认逐单独立
    事务」）；初始状态非 `PLANNED` 的 `StateConflict` 则原样上抛（见模块 docstring）。

    `lock_version` 是调用方**读的时候**看到的版本号（spec `data-model`「并发确认仅一方
    成功」：先提交者推进版本、后提交者被拒）。给 `None` 表示调用方未携带版本（逻辑测试 /
    未接版本回环的调用方），此时按 `job_order.lock_version` 原值推进 —— 与
    `allocate` 的 `bump_lock_version(order, expected=order.lock_version)` 同一口径：只取
    「唯一一处定义」的推进语义，不额外校验。

    本函数不 `commit`：事务边界属于端点（`deps.get_db` 只回滚不提交，与
    `allocate_batch_plans` 同一口径）。批量确认端点逐单 `commit`，单失败时把 `PLANNED`
    那一步一并提交。
    """
    # 1. 乐观锁（spec `data-model`「并发确认仅一方成功」）：守卫在 try 之外、且在状态
    #    迁移**之前** —— 版本冲突与源状态错一样，是「调用方过期」，不是「执行失败」，
    #    不该被降级吞掉。先判版本再动状态，后到的陈旧写入在改任何东西之前就被拒（409）。
    bump_lock_version(
        job_order, expected=job_order.lock_version if lock_version is None else lock_version
    )

    # 2. 确认（决策权在人，二次确认卡已在端点 / 前端通过）：PLANNED → CONFIRMED。
    #    `assert_transition` 在这里、在 try 之外 —— 见模块 docstring「幂等 / 守卫」。
    job_order.status = assert_transition(job_order.status, JobStatus.CONFIRMED)
    job_order.confirmed_by_id = operator_id
    job_order.confirmed_at = executed_at
    # 执行侧（17 §4.1）：实际落位 = 目标库位（入库 / 移库）或源库位（出库）。
    job_order.actual_location_code = target_location_code or source_location_code
    job_order.actual_qty = actual_qty if actual_qty is not None else job_order.qty
    session.flush()

    # 2. 执行：写台账 → cap 增量 → CONFIRMED → EXECUTED，同一 savepoint。
    try:
        with session.begin_nested():
            ledger = write_ledger(
                session,
                job_order=job_order,
                source_location_code=source_location_code,
                target_location_code=target_location_code,
                operator_id=operator_id,
                executed_at=executed_at,
                pick_path_json=pick_path_json,
                plan_json=plan_json,
                degraded=degraded,
                degrade_reason=degrade_reason,
            )
            apply_increment(session, ledger=ledger, snapshot=snapshot)
            job_order.status = assert_transition(job_order.status, JobStatus.EXECUTED)
            job_order.executed_at = executed_at
            session.flush()
    except Exception:
        # savepoint 已整体回滚（台账、cap 增量、EXECUTED 迁移都没了），回边退回 PLANNED。
        _demote_to_planned(session, job_order)
    return job_order
