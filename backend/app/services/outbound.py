"""出库管线（7 步）：导入 DO → 筛选 → 勾选 → 批量生成顺路取 → 逐单处置 → 批量确认 → 写出库台账 + 后验。15 §2.2。只读派生：不触发评分引擎、不重新决定落位。

本模块落「批量确认 → 写出库台账」这一段的确认编排（design.md D5）：
`confirm_outbound` 把一张 `PLANNED` 的出库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写出库台账
（无源库位、无目标库位，拣货路径记 `pick_path_json`）与 cap 增量（按拣货路径逐巷 ↓，D7）。
出库是只读派生 —— 拣货路径来自顺路取方案（操作员可微调后确认），本编排**不重新决定落位**，
只把最终拣货路径固化进台账。后验在 `verify.py`。
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.core.errors import ValidationBlocked
from app.engine.factors import InventoryProfile
from app.models.job import JobOrder, PlanKind, RecommendationPlan
from app.models.linkage import Snapshot
from app.services.confirm import _confirm_and_execute
from app.services.verify import (
    DEFAULT_CONCENTRATION_N,
    concentration_aisle_count,
    run_verification,
)


def derive_pick_sequence(
    *,
    material_code: str,
    do_no: str,
    profile: InventoryProfile,
    n: int = DEFAULT_CONCENTRATION_N,
) -> dict:
    """顺路取**只读派生**：按巷道聚合既有库存，产出拣货顺序 + 加权集中度（15-03 §3.2）。

    纯函数（design.md D1）：不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`。
    输入 = `SnapshotIndex.profile(material_code)` 的现状分布，输出 `17` §10.2 形状：

    ```json
    {
      "do_no": "…",
      "pick_sequence": [ {"aisle": "01", "qty": 3, "batches": ["GJP…"]}, … ],
      "weighted_concentration": 3,
      "threshold_n": 5,
      "exceeded": false
    }
    ```

    - `pick_sequence`：取自 `profile.plates_by_aisle`（每巷库存量 = 拣货量，**现状分布**，
      不跨巷分配）+ `profile.batches_by_aisle`（每巷批号，升序），按 `aisle` **升序**
      （顺路 = 库位号序走仓，`17` §10.2 示例即升序）。
    - `weighted_concentration`：复用 `concentration_aisle_count`（80% 降序累加，不重写）。
    - `exceeded`：`weighted_concentration > n`，**仅高亮不阻断**（15-03 §6.2）。
    - 空档案（货未入库）→ 空 `pick_sequence`、集中度 0；是否分列 `not_in_stock` 由
      端点判定，不在这里。

    确定性：同样 profile 必得同样输出（无随机、无大模型）。
    """
    pick_sequence = [
        {
            "aisle": aisle,
            "qty": qty,
            "batches": sorted(profile.batches_by_aisle.get(aisle, frozenset())),
        }
        for aisle, qty in sorted(profile.plates_by_aisle.items())
    ]
    pick_qty_by_aisle = dict(profile.plates_by_aisle)
    weighted_concentration = concentration_aisle_count(
        pick_qty_by_aisle=pick_qty_by_aisle
    )
    return {
        "do_no": do_no,
        "pick_sequence": pick_sequence,
        "weighted_concentration": weighted_concentration,
        "threshold_n": n,
        "exceeded": weighted_concentration > n,
    }


def _current_pick_sequence(session: Session, job_order: JobOrder) -> list | None:
    """该单「当前方案」的 `pick_sequence`（D4 回退：未微调 = 接受推荐）。

    当前方案 = `plan_kind=PICK` 且 `id` 最大的一行（`RecommendationPlan.job_order_id` 不唯一，
    视图推进会追加行）；无方案返回 `None`。与路由层 `_current_pick_plan` 同一口径，但这里
    只取 `payload_json["pick_sequence"]`，因为确认编排要的是拣货路径本身。
    """
    plan = session.scalars(
        sa.select(RecommendationPlan)
        .where(
            RecommendationPlan.job_order_id == job_order.id,
            RecommendationPlan.plan_kind == PlanKind.PICK,
        )
        .order_by(RecommendationPlan.id.desc())
        .limit(1)
    ).first()
    if plan is None:
        return None
    return plan.payload_json.get("pick_sequence")


def confirm_outbound(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    source_location_code: str | None = None,
    actual_qty: int | None = None,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    pick_path_json: list | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """出库确认→执行→后验：按最终拣货路径出库，写台账（`pick_path_json`）+ cap 增量，迁
    `EXECUTED` 再同步后验。

    返回同一个 `job_order`（`VERIFIED` / `VERIFY_FAILED`，或失败回退的 `PLANNED`）。
    台账矩阵：出库无源库位、无目标库位（D4）—— 多巷无单一源，拣货分布记 `pick_path_json`
    （巷道序）。`pick_path_json` 为 `None` 时回退读该单当前方案的 `pick_sequence`
    （未微调 = 接受推荐），仍取不到抛 `ValidationBlocked`（不猜测拣货路径）。

    `source_location_code` 形参保留仅为签名兼容（D4 改可空）：出库无单一源库位，本编排
    不再落单一源库位。`lock_version` 透传给确认编排的乐观锁（见 `confirm._confirm_and_execute`）。
    """
    if pick_path_json is None:
        pick_path_json = _current_pick_sequence(session, job_order)
    if not pick_path_json:
        raise ValidationBlocked(
            f"作业单 #{job_order.id} 无最终拣货路径 —— 出库确认需拣货路径（pick_path 或该单"
            "当前顺路取方案），取不到时不猜测拣货路径",
            detail={"job_order_id": job_order.id},
        )
    result = _confirm_and_execute(
        session,
        job_order=job_order,
        operator_id=operator_id,
        executed_at=executed_at,
        source_location_code=None,
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
