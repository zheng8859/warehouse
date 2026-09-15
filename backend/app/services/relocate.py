"""移库管线（7 步）：KPI 识别偏离 → 筛选 → 勾选 → 批量生成收拢方案 → 逐单处置 → 批量确认执行 → 写移库台账 + 后验刷新。15 §2.3。目标巷道 = 主巷道；不改批号。

本模块落两段：

- **收拢方案只读派生**（design.md D2）：`derive_consolidation_plan` 是纯函数，按批号聚合
  散落板、定主巷道（= 该物料库存最集中的巷道）+ 三重校验（cap 充足 / 批号不变 /
  收拢后跨巷道数下降）+ 降级链（主巷道 → 次选 → 移出批量）。不触会话、不调
  `engine.invoke`、不写 `InventoryItem`/`Ledger`。
- **批量确认执行 → 写移库台账**（design.md D5）：`confirm_relocate` 把一张 `PLANNED` 的
  移库单经 `CONFIRMED` 迁到 `EXECUTED`，同事务写移库台账（源 + 目标都有）与 cap 增量
  （源 ↓、目标 ↑）。**移库不改批号**（CLAUDE.md §四）—— 台账只记源 / 目标库位，批号
  原样照抄。后验在 `verify.py`。
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.core.errors import ValidationBlocked
from app.engine.allocator import to_occupied_cells
from app.engine.factors import InventoryProfile, load_snapshot_index
from app.models.job import JobOrder
from app.models.linkage import Snapshot
from app.services.confirm import _confirm_and_execute
from app.services.verify import run_verification

#: ④ 移库多方案的代价模型常数（doc 10 §六 示例反推）：15 板/车次、5 车次/小时。
#: 属「辅助参考」（冷路径产物「辅助参考，不是精确诊断」），只用于多方案对比的代价估算，
#: 不是落位决策的精确成本。板-格换算与车次口径本阶段未定（CLAUDE.md §十一 D14 待确认项），
#: 这里从示例值反推一个确定性的估算口径。
PLATES_PER_TRIP = 15
TRIPS_PER_HOUR = 5


@dataclass(frozen=True)
class ConsolidationPlanResult:
    """收拢方案派生的结果：出方案（`plan`）或移出批量（`moved_out_reason`），**恰有其一**。

    两个字段都判空（或都给）的结果没法被消费方解读：端点要按它决定「写 `RecommendationPlan`
    并迁 `PLANNED`」还是「记入 `moved_out[]`」。故这条不自洽在**构造时**就拦掉（与
    `FactorOutcome` 的 `__post_init__` 同一手法），而不是等调用方拿到一个 `plan=None` 的
    方案或一个空 `moved_out_reason` 才炸。
    """

    plan: dict | None = None
    moved_out_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.plan is None) == (self.moved_out_reason is None):
            raise ValueError(
                "ConsolidationPlanResult 必须恰有其一：plan（收拢方案）或 "
                "moved_out_reason（移出批量的降级原因）"
            )
        if self.moved_out_reason is not None and not self.moved_out_reason.strip():
            raise ValueError("移出批量必须写明原因（降级不静默）")


def _target_candidates(profile: InventoryProfile) -> list[str]:
    """收拢目标候选：物料级板数降序、并列按巷道号升序，取前二（主巷道 + 次选）。

    D1 的口径：主巷道 = `plates_by_aisle` 的 argmax，并列取巷道号文本升序最小；次选 =
    板数第二多。降级链只有两档（主巷道 → 次选 → 移出批量，15-04 §4.3），故取 `[:2]`，
    不给「再往下退一档」留余地 —— 那会把「收拢到第三集中的巷道」这种既非最优、又可能
    违反集中度判据的落点放进方案。
    """
    ranked = sorted(
        profile.plates_by_aisle.items(), key=lambda kv: (-kv[1], kv[0])
    )
    return [aisle for aisle, _ in ranked[:2]]


def _consolidate_to(
    *,
    material_code: str,
    batch_no: str,
    profile: InventoryProfile,
    batch_plates_by_aisle: Mapping[str, int],
    available: Mapping[str, int],
    target: str,
    cartons_per_pallet: int | None = None,
    moving_batches: Set[str] | None = None,
    batch_locations_by_aisle: Mapping[str, Sequence[tuple[str, int]]] = (),
    material_locations_by_aisle: Mapping[str, Sequence[str]] = (),
) -> tuple[dict | None, tuple[str, str] | None]:
    """对**一个**目标巷道试算收拢方案；返回 `(plan, failure)`，恰一非空。

    `failure` = `(kind, message)`，`kind` ∈ {`"cap"`, `"concentration"`}。把成因分两档
    而不是统一成一句文案，是因为主巷道的两种失败**去向不同**：`cap` 触发降级次选，
    `concentration` 直接移出批量（降级目标巷道救不了「已集中于主巷道 / 散落板与其他
    物料共占」—— 那两类都不是容量问题）。

    - `from_aisles` = `batch_plates_by_aisle` 里**非目标**的巷道，按巷道号升序。
    - `plates` = 散落板总数 = `from_aisles` 板数之和。
    - `after` = `profile.cross_aisle_count` − 收拢后变空的巷道数（**物料级**）。「变空」
      判据：一条非目标巷道的批号集是 `moving_batches` 的子集 —— 即该巷该物料的每一批
      都在本次被收拢的散批里、一并搬出。「按物料收拢散批」（用户 2026-09-15 确认）——
      多批交织时单批收拢搬不空共享巷道，只有整料一并收拢才数得准 `after`；这是缺口 1
      的修复（旧判据「本批是该巷唯一物料板」会把 GJP2571421/1322 这类交织散批误判
      移出批量）。`moving_batches` 缺省退化为 `{batch_no}`（单批收拢 = 本批是该巷唯一批）。
    - cap 充足 = `available[target] >= plates_cells`。

    **单位口径**（D14 确认 2026-09-15）：`batch_plates_by_aisle` 里的数其实是**箱数**
    （`inventory_items.qty`），而 `available` 是**格数**（1 板 = 1 格）。比较前用
    `to_occupied_cells` 把箱数换成格数，否则 `cartons_per_pallet=102` 的料号会把
    「298 格」误判成「30361 板」，容量放大约 102 倍、主巷道被错误判满。
    """
    from_aisles = sorted(a for a in batch_plates_by_aisle if a != target)
    plates = sum(batch_plates_by_aisle[a] for a in from_aisles)
    plates_cells = to_occupied_cells(plates, cartons_per_pallet=cartons_per_pallet)
    before = profile.cross_aisle_count
    moving = frozenset(moving_batches) if moving_batches is not None else frozenset({batch_no})
    emptied = sum(
        1
        for aisle, batches in profile.batches_by_aisle.items()
        if aisle != target and batches and batches <= moving
    )
    after = before - emptied

    avail = available.get(target, 0)
    if avail < plates_cells:
        return None, ("cap", f"巷道 {target} 容量不足（可用 {avail}，需 {plates_cells} 板）")
    if after >= before:
        return None, (
            "concentration",
            f"收拢后同物料跨巷道数不低于收拢前（{after} ≥ {before}）——"
            "散落板与其他物料/批号共占，或本批未随整料一并收拢、移出后巷道不变空",
        )

    # 逐格真实源库位（缺口 2）：`from_aisles` 里该批的库存行，按库位号升序展平成
    # `{location_code, qty}`（箱数）—— 执行侧据此逐格扣减，不再编造「巷道 + 固定后缀」。
    source_locations = [
        {"location_code": location_code, "qty": qty}
        for aisle in from_aisles
        for location_code, qty in batch_locations_by_aisle.get(aisle, ())
    ]

    # 目标库位 = 目标巷道内该物料的既有库位（确定性取最低库位号）。目标巷道来自
    # `plates_by_aisle`（该巷该物料有板数），必然有库存行；无行只可能是取数缺口，
    # 属数据自相矛盾 —— 响亮失败，不编造一个库位。
    target_candidates = material_locations_by_aisle.get(target, ())
    if not target_candidates:
        return None, (
            "cap",
            f"目标巷道 {target} 无该物料的既有库位可取 —— 目标库位无从解析，不能编造库位",
        )
    target_location = target_candidates[0]

    plan = {
        "batch_no": batch_no,
        "material_code": material_code,
        "from_aisles": from_aisles,
        "target_aisle": target,
        "plates": plates_cells,
        "source_locations": source_locations,
        "target_location": target_location,
        "expected_cross_aisle": {"before": before, "after": after},
        "batch_unchanged": True,
    }
    return plan, None


def derive_consolidation_plan(
    *,
    material_code: str,
    batch_no: str,
    profile: InventoryProfile,
    batch_plates_by_aisle: Mapping[str, int],
    available: Mapping[str, int],
    cartons_per_pallet: int | None = None,
    moving_batches: Set[str] | None = None,
    batch_locations_by_aisle: Mapping[str, Sequence[tuple[str, int]]] = (),
    material_locations_by_aisle: Mapping[str, Sequence[str]] = (),
) -> ConsolidationPlanResult:
    """收拢方案**只读派生**（15-04 §4.2 / design.md D2）：主巷道 + 三重校验 + 降级链。

    纯函数：不触会话、不调 `engine.invoke`、不写 `InventoryItem`/`Ledger`。输入 = 现状分布
    （`SnapshotIndex.profile(material_code)` 的 `InventoryProfile`）+ 批号级取数
    （`batch_plates_by_aisle`）+ 逐格库位取数（`batch_locations_by_aisle` /
    `material_locations_by_aisle`，缺口 2 的真实库位来源）+ 各候选巷道的可用格数
    （`available`），输出 `17` §10.3 形状（或移出批量的降级原因）。

    三重校验：① cap 充足 ② 批号不变（`batch_unchanged`，结构性恒真）③ 集中度改善
    （`after < before`，**物料级**判据见 `_consolidate_to`）。降级链：主巷道 cap 不足 →
    次选（cap 充足且 `after < before` 仍成立，方案记 `degrade_reason`）→ 仍不可行 →
    `moved_out_reason`（降级不静默）。

    确定性：同样输入必得同样输出（无随机、无大模型）。
    """
    candidates = _target_candidates(profile)
    if not candidates:
        return ConsolidationPlanResult(
            moved_out_reason="该物料在快照中无库存分布，无候选主巷道可收拢"
        )

    main_aisle = candidates[0]
    main_plan, main_fail = _consolidate_to(
        material_code=material_code,
        batch_no=batch_no,
        profile=profile,
        batch_plates_by_aisle=batch_plates_by_aisle,
        available=available,
        target=main_aisle,
        cartons_per_pallet=cartons_per_pallet,
        moving_batches=moving_batches,
        batch_locations_by_aisle=batch_locations_by_aisle,
        material_locations_by_aisle=material_locations_by_aisle,
    )
    if main_plan is not None:
        return ConsolidationPlanResult(plan=main_plan)

    kind, message = main_fail  # type: ignore[misc]  # main_fail 非空（plan 为 None）
    if kind == "concentration":
        # 集中度不改善 → 移出批量（不降级：换个目标巷道救不了「已集中 / 共占」）。
        return ConsolidationPlanResult(moved_out_reason=message)
    if len(candidates) < 2:
        # 只有一条巷道且 cap 不足：无次选可退，直接移出批量。
        return ConsolidationPlanResult(moved_out_reason=message)

    second_aisle = candidates[1]
    second_plan, second_fail = _consolidate_to(
        material_code=material_code,
        batch_no=batch_no,
        profile=profile,
        batch_plates_by_aisle=batch_plates_by_aisle,
        available=available,
        target=second_aisle,
        cartons_per_pallet=cartons_per_pallet,
        moving_batches=moving_batches,
        batch_locations_by_aisle=batch_locations_by_aisle,
        material_locations_by_aisle=material_locations_by_aisle,
    )
    if second_plan is not None:
        second_plan["degrade_reason"] = (
            f"主巷道 {main_aisle} 容量不足，降级至次选巷道 {second_aisle}"
        )
        return ConsolidationPlanResult(plan=second_plan)

    _, second_message = second_fail  # type: ignore[misc]
    return ConsolidationPlanResult(
        moved_out_reason=(
            f"主巷道 {main_aisle} 容量不足（{message}）；"
            f"次选巷道 {second_aisle} 亦不可行（{second_message}），移出批量"
        )
    )


def build_relocate_plans(
    *,
    plates_by_aisle: Mapping[str, int],
    available: Mapping[str, int],
    cross_aisle_threshold: int = 5,
) -> dict[str, Any]:
    """④ 移库多方案（规则算、纯函数、确定性）：激进 / 均衡 / 保守 三档 + 量化代价 + 三重校验。

    事实来源：10-AI 辅助能力设计 §六（④ 多方案对比 + 量化代价，LLM 只叙事）
              openspec/changes/ai-assist/design.md D8（多方案规则算，复用三重校验）

    输入 = 物料级板数分布（`plates_by_aisle`）+ 各巷可用格数（`available`），输出三档
    收拢方案的**试算**（不触会话、不写 `JobOrder`）。三档按「激进程度」递减：

    - **激进**：全部收拢到前二巷道（主巷 + 次选，与 `_target_candidates` 同源），
      跨巷道数最小、搬移量最大 —— 过度达成 ≤阈值，代价最高。
    - **均衡（推荐）**：收拢到前 `cross_aisle_threshold` 巷道，**恰好达成**同物料跨巷道
      ≤阈值的验收指标（`18` §十 的统一指标 `same_material_cross_aisle_max`），搬移量适中。
    - **保守**：只清长尾（板数最少的后 `max(1, n//3)` 条巷道），搬移量最小，
      可能**未达** ≤阈值。

    三重校验（与 `_consolidate_to` 同口径，`17` §10.3 的移库方案三判据）：

    ① cap 充足 —— 目标巷道可用格数之和 ≥ 待搬板数；
    ② 批号不变 —— 移库不改批号（红线），结构性恒真；
    ③ 集中度改善 —— 收拢后跨巷道数 < 收拢前。

    `valid = ① and ② and ③`。量化代价（板数 / 车次 / 时长）用 §六 示例反推的估算口径
    （`PLATES_PER_TRIP` / `TRIPS_PER_HOUR`），属「辅助参考，不是精确诊断」，只用于三档
    对比，不是落位决策的精确成本。
    """
    ranked = sorted(plates_by_aisle.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(ranked)
    total = sum(plates_by_aisle.values())

    aggressive_keep = min(2, n)
    balanced_keep = min(cross_aisle_threshold, n)
    # 只清长尾 = 清掉板数最少的后三分之一巷道（至少 1 条）；夹取下界保证「保守」不比
    # 「均衡」更激进（两者在 n 较小时重合，属正常 —— 分布太散没有第三档的空间）。
    conservative_keep = max(balanced_keep, min(n, n - max(1, n // 3)))

    plans: list[dict[str, Any]] = []
    for name, keep_count in (("激进", aggressive_keep), ("均衡", balanced_keep), ("保守", conservative_keep)):
        keep = [aisle for aisle, _ in ranked[:keep_count]]
        clear = [aisle for aisle, _ in ranked[keep_count:]]
        plates_to_move = sum(plates_by_aisle[a] for a in clear)
        cross_before, cross_after = n, keep_count

        cap_ok = sum(available.get(a, 0) for a in keep) >= plates_to_move
        batch_unchanged = True  # 移库不改批号（红线），结构性恒真。
        concentration_improved = cross_after < cross_before

        trips = math.ceil(plates_to_move / PLATES_PER_TRIP) if plates_to_move else 0
        plans.append(
            {
                "name": name,
                "target_aisles": keep,
                "clear_aisles": clear,
                "cross_aisle": {"before": cross_before, "after": cross_after},
                "plates_to_move": plates_to_move,
                "trips": trips,
                "hours": round(trips / TRIPS_PER_HOUR, 1),
                "cap_ok": cap_ok,
                "batch_unchanged": batch_unchanged,
                "concentration_improved": concentration_improved,
                "valid": cap_ok and batch_unchanged and concentration_improved,
            }
        )

    return {
        "current": {
            "aisles": [aisle for aisle, _ in ranked],
            "cross_aisle": n,
            "plates": total,
        },
        "plans": plans,
    }


def confirm_relocate(
    session: Session,
    *,
    job_order: JobOrder,
    operator_id: int,
    executed_at: datetime,
    source_locations: Sequence[Mapping[str, object]],
    target_location_code: str,
    snapshot: Snapshot | None = None,
    lock_version: int | None = None,
    plan_json: dict | None = None,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> JobOrder:
    """移库确认→执行→后验：逐格源库位移到目标库位，写移库台账 + cap 增量，迁 `EXECUTED` 再同步后验。

    返回同一个 `job_order`（`VERIFIED` / `VERIFY_FAILED`，或失败回退的 `PLANNED`）。
    台账矩阵：移库源库位与目标库位都有（15 附录A）。

    **单一台账行 + 逐格源库位随 `plan_json` 固化**（缺口 2）：`ledgers` 的唯一约束
    `uq_ledgers_job_order_id_reversal(job_order_id, is_reversal)` 只允许一单一条正常台账行，
    而散落板跨多个库位 —— 故台账行的 `source_location_code` 落**代表源库位**（逐格清单里的
    最低库位号），完整逐格清单存进 `plan_json.source_locations`，`apply_increment` 据此
    逐格扣减。实际执行量 = 逐格箱数之和（不是作业单上的批号总箱数 —— 目标巷道里本就
    集中的那部分不搬）。

    移库后验是**相对阈值**（移库后跨巷道 < 移库前），故在增量**之前**先取「移库前」的同物料
    跨巷道数，交给后验编排与「移库后」比（`15-04` §8.1）。`lock_version` 透传给确认编排的
    乐观锁（见 `confirm._confirm_and_execute`）。
    """
    if not source_locations:
        raise ValidationBlocked(
            f"作业单 #{job_order.id} 的移库源库位清单为空 —— 散落板跨多个库位，"
            "必须给出逐格真实源库位（不能编造单一源库位）",
            detail={"job_order_id": job_order.id},
        )
    representative_source = str(source_locations[0]["location_code"])
    total_moved = sum(int(entry["qty"]) for entry in source_locations)
    executed_plan = {
        **(plan_json or {}),
        "source_locations": [dict(entry) for entry in source_locations],
        "target_location": target_location_code,
    }

    cross_aisle_before: int | None = None
    if snapshot is not None:
        pre = load_snapshot_index(session, snapshot_id=snapshot.id)
        cross_aisle_before = pre.profile(job_order.material_code).cross_aisle_count

    result = _confirm_and_execute(
        session,
        job_order=job_order,
        operator_id=operator_id,
        executed_at=executed_at,
        source_location_code=representative_source,
        target_location_code=target_location_code,
        actual_qty=total_moved,
        snapshot=snapshot,
        lock_version=lock_version,
        plan_json=executed_plan,
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
