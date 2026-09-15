"""出库管线（7 步）：导入 DO → 筛选 → 勾选 → 批量生成顺路取 → 逐单处置 → 批量确认 → 写出库台账 + 后验。15 §2.2。只读派生：不触发评分引擎、不重新决定落位。

本模块落「批量确认 → 写出库台账」这一段的确认编排（design.md D5）：
`confirm_outbound` 把一张 `PLANNED` 的出库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写出库台账
（无源库位、无目标库位，拣货路径记 `pick_path_json`）与 cap 增量（按拣货路径逐巷 ↓，D7）。
出库是只读派生 —— 拣货路径来自顺路取方案（操作员可微调后确认），本编排**不重新决定落位**，
只把最终拣货路径固化进台账。后验在 `verify.py`。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    qty: int,
    n: int = DEFAULT_CONCENTRATION_N,
    batch_locations_by_aisle: Mapping[str, Mapping[str, Sequence[tuple[str, int]]]]
    | None = None,
) -> dict:
    """顺路取**只读派生**：按批 FIFO、按巷集中，拣货量封顶到订单交货量。

    纯函数（design.md D1）：不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`。
    输入 = `SnapshotIndex.profile(material_code)` 的现状分布 + 订单交货量 `qty`，输出
    `17` §10.2 形状再加一个 `batch_no`（FIFO 最早批，供端点回写 `order.batch_no`；
    本函数不写库，`batch_no` 由端点从结果里剥离后再落方案 payload）：

    ```json
    {
      "do_no": "…",
      "pick_sequence": [
        {"aisle": "01", "qty": 3, "batches": ["GJP…"],
         "locations": [{"location_code": "010101", "qty": 3, "batch_no": "GJP…"}]}, …
      ],
      "weighted_concentration": 3,
      "threshold_n": 5,
      "exceeded": false,
      "batch_no": "GJP…"
    }
    ```

    - **FIFO 批号序**：批号字典序 = 生产日期序（`GJP + YY + M + DD + 线 + 班`，日期在
      4~8 位），故 `sorted(批号)` 即最早在前。逐批贪心，最早批不足 `qty` 顺延到下一批。
    - **按批集中取巷**：同一批内按「单巷量从大到小、并列巷号升序」取巷（尽量少巷道）；
      单巷就够 → 只出这一条巷道（业务口径：最早批多巷且各巷都够交货量时只显示一条）。
    - **封顶**：累加到 `qty` 为止，末巷截断（不把整份库存 dump 成拣货量 —— 那会让确认时
      按巷道总量扣减、一次清空该料号全部库存）。
    - `pick_sequence` 按 `aisle` 升序（顺路 = 库位号序走仓，`17` §10.2 示例即升序），每巷
      一条：`qty` = 该巷被拣的量、`batches` = 该巷被拣到的批号（升序）。
    - **库位级下钻（缺口 3）**：`batch_locations_by_aisle`（批号 → 巷道 → 逐格库位号+箱数）
      提供时，把每条被拣巷道的拣货量解析成逐格 `locations`（FIFO 批序 → 批内库位号升序）；
      不传 → 每巷 `locations = []`（仍只到巷道级）。`locations` 只作「从哪个库位号出」的
      可视化与审计，**不改 cap 扣减**（扣减仍巷道级，`to_occupied_cells` 对巷道总量取整）。
    - `weighted_concentration` 复用 `concentration_aisle_count`（对**拣货分布**算 80%
      降序累加，不是整份库存分布）。
    - `exceeded`：`weighted_concentration > n`，**仅高亮不阻断**（15-03 §6.2）。
    - 空档案（货未入库）→ 空 `pick_sequence`、`batch_no=None`、集中度 0；是否分列
      `not_in_stock` 由端点判定，不在这里。

    确定性：同样 profile + qty（+ batch_locations_by_aisle）必得同样输出（无随机、无大模型）。
    """
    # 批号 → [(巷道, 数量), …]，批号升序（FIFO）、批内单巷量降序（并列巷号升序）——
    # 贪心取大巷即「尽量少巷道」，单巷够用即只出一条。
    by_batch = {
        batch: sorted(aisles.items(), key=lambda kv: (-kv[1], kv[0]))
        for batch, aisles in sorted(profile.qty_by_batch_by_aisle.items())
    }
    pick_by_aisle: dict[str, dict] = {}
    taken: dict[str, dict[str, int]] = {}  # batch → aisle → 该批在该巷被拣的量（缺口 3）
    remaining = qty
    batch_no: str | None = None
    for batch, aisles in by_batch.items():
        if remaining <= 0:
            break
        if batch_no is None:
            batch_no = batch  # FIFO 最早批（第一个真正被拣的批号）
        for aisle, avail in aisles:
            if remaining <= 0:
                break
            if avail <= 0:
                continue
            take = min(avail, remaining)
            entry = pick_by_aisle.setdefault(aisle, {"qty": 0, "batches": set()})
            entry["qty"] += take
            entry["batches"].add(batch)
            taken.setdefault(batch, {})[aisle] = taken.get(batch, {}).get(aisle, 0) + take
            remaining -= take

    pick_sequence = [
        {
            "aisle": aisle,
            "qty": entry["qty"],
            "batches": sorted(entry["batches"]),
            "locations": _resolve_pick_locations(
                aisle, entry["batches"], taken, batch_locations_by_aisle
            ),
        }
        for aisle, entry in sorted(pick_by_aisle.items())
    ]
    pick_qty_by_aisle = {aisle: entry["qty"] for aisle, entry in pick_by_aisle.items()}
    weighted_concentration = concentration_aisle_count(
        pick_qty_by_aisle=pick_qty_by_aisle
    )
    return {
        "do_no": do_no,
        "pick_sequence": pick_sequence,
        "weighted_concentration": weighted_concentration,
        "threshold_n": n,
        "exceeded": weighted_concentration > n,
        "batch_no": batch_no,
    }


def _resolve_pick_locations(
    aisle: str,
    batches: set[str],
    taken: Mapping[str, Mapping[str, int]],
    batch_locations_by_aisle: Mapping[str, Mapping[str, Sequence[tuple[str, int]]]]
    | None,
) -> list[dict]:
    """把一条被拣巷道的拣货量下钻到逐格库位号（缺口 3）。

    顺序 = FIFO 批号序（`batches` 升序）→ 批内库位号升序；逐个消费 `(location_code, qty)`
    直到凑满该批在该巷被拣的量（`taken[batch][aisle]`）。`batch_locations_by_aisle` 为
    `None`（旧调用方 / 无库位级取数）→ 空列表（仍只到巷道级）。
    """
    if batch_locations_by_aisle is None:
        return []
    locations: list[dict] = []
    for batch in sorted(batches):
        need = taken.get(batch, {}).get(aisle, 0)
        if need <= 0:
            continue
        for location_code, qty in batch_locations_by_aisle.get(batch, {}).get(aisle, ()):
            if need <= 0:
                break
            take = min(qty, need)
            locations.append(
                {"location_code": location_code, "qty": take, "batch_no": batch}
            )
            need -= take
    return locations


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
    # 拣货量不足订单量 → 阻断过账。顺路取只在可用库存里封顶，可用量不足时不设标志
    # （`exceeded` 是「加权集中度 > 5 巷」，与库存是否充足无关），所以要在确认时把
    # 「拣货路径总量 < 订单量」这条显式拦住 —— 否则台账会按满额 `job_order.qty` 记
    # 出库、扣减却只按拣货路径扣，账实不一致（库存不足不得带病过账）。
    picked_qty = sum(int(entry.get("qty") or 0) for entry in pick_path_json)
    if picked_qty < job_order.qty:
        raise ValidationBlocked(
            f"作业单 #{job_order.id}（{job_order.order_no}）拣货量不足订单量：顺路取仅覆盖 "
            f"{picked_qty} 箱，订单需 {job_order.qty} 箱，差 {job_order.qty - picked_qty} 箱 —— "
            "库存不足，阻断过账（不写台账、不扣库存）",
            detail={
                "job_order_id": job_order.id,
                "picked_qty": picked_qty,
                "required_qty": job_order.qty,
            },
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
