"""台账写入 —— 唯一入口，由引擎 / 作业流自动调用，不向角色开放手动入口。

事实来源：15-入库出库移库与后验流程设计 §6.3（二次确认后写台账）、附录A（三类台账字段矩阵）
          17-数据模型设计 §4.3（Ledger 实体）
          openspec/changes/transaction-base/design.md D3（is_reversal 反向行）、D5（唯一写入入口）
          spec `transaction-base`「台账与 cap 增量同事务」

## 为什么只有一个入口

台账只有一套（CLAUDE.md §四红线），它是 cap 与库存分布增量的唯一来源。多一个写入方，
就多一个「绕过台账改 cap / 库存」的洞口；而 `ledger.write` 在权限矩阵里是 AUTO_ONLY
（`app/api/permissions.py`），从角色侧本就关死。本函数是**服务层**的唯一写入点 ——
确认编排、冲正编排都调它，各自决定 source / target 与 `is_reversal`。

## 边界：写入即固化，事务边界不在本函数

- 本函数 `session.add` + `session.flush`，**不 commit**：flush 让 NOT NULL / 唯一约束 /
  `_LEDGER_LOCATION_CHECK` 在事务内当场生效，调用方（确认 / 冲正编排）才能在「写台账失败」
  时整体回滚（design.md D7 用事务回滚而非应用补偿）。commit 属于端点。
- 台账表没有 `updated_at`、没有删除接口，`add` 之后即固化 —— 本函数只做「落一行」，
  不提供任何回填 / 改写的钩子。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.enums import LedgerType
from app.core.errors import ValidationBlocked
from app.models.job import JobOrder, Ledger


def write_ledger(
    session: Session,
    *,
    job_order: JobOrder,
    source_location_code: str | None = None,
    target_location_code: str | None = None,
    operator_id: int,
    executed_at: datetime,
    pick_path_json: list | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
    is_reversal: bool = False,
) -> Ledger:
    """落一条台账行（正常行或 `is_reversal` 反向行），写入即固化。

    `source_location_code` / `target_location_code` 由调用方按 `_LEDGER_LOCATION_CHECK`
    的矩阵给出（入库只有目标、出库只有源、移库两者都有）—— 本函数不按 `job_type` 反推，
    因为「填错哪一格」正是 DB CHECK 要拦的错，反推会把错误静默吞成「看着合法的正确值」。

    `is_reversal=True` 只由冲正编排给出：反向行与正常行同落 `ledgers` 表、同字段、同口径，
    区别仅在 `is_reversal` 与「cap 增量的方向被 `apply_increment` 反向」（design.md D3）。
    """
    if job_order.batch_no is None:
        # 台账的 batch_no 是 NOT NULL（15 附录A 三类都有）。在写入前显式拦掉，
        # 比让 flush 抛一条 "NOT NULL constraint failed: ledgers.batch_no" 更指向真因：
        # 「单据建立时没生成批号」，而不是「这一行漏了某个字段」。
        raise ValidationBlocked(
            f"作业单 #{job_order.id} 无生产批号，无法写台账 —— "
            "批号由系统在入库单建立时按生产批规则生成，缺失属登记侧未完成，"
            "不写出一条没有批号、无法追溯的台账行",
            detail={"job_order_id": job_order.id},
        )

    ledger = Ledger(
        warehouse_id=job_order.warehouse_id,
        job_order_id=job_order.id,
        is_reversal=is_reversal,
        ledger_type=LedgerType(job_order.job_type.value),
        order_no=job_order.order_no,
        material_code=job_order.material_code,
        material_name=job_order.material_name,
        batch_no=job_order.batch_no,
        qty=job_order.qty,
        source_location_code=source_location_code,
        target_location_code=target_location_code,
        pick_path_json=pick_path_json,
        plan_json=plan_json,
        operator_id=operator_id,
        executed_at=executed_at,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )
    session.add(ledger)
    # flush 让 `_LEDGER_LOCATION_CHECK` / 唯一约束当场生效 —— 调用方据此判「写台账失败」。
    session.flush()
    return ledger
