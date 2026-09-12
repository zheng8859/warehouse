"""当日队列批量竞争分配（贪心）。`14` §3.4。全局最优（整数规划 / 局部交换）属路线图。

事实来源：`14` §3.1（队列输入）、§3.2（排序键 = 谁先挑稀缺容量）、§3.4（步 1 可行集 →
          步 2 选最高分 → 步 3 扣减 → 步 4 空则降级 → 步 5 回溯预测）、§3.5（四级链、
          降级不静默、A 类告警）、§一 第二条原则（资源仲裁与位置选择分离）
          `17` §3.4（`AisleCap` 三列）、§10.1（六因子 / 权重 / `scores`）、§10.7（响应）、
          §11（数据隔离）
          `15` §11.1（分配失败停留 `PENDING`，可重试）
          `openspec/changes/recommendation-engine/design.md` D1（模块切分与单向调用链）、
          D2（全序 / 量化比较 / 时钟注入）、D4（内存扣减 · 不落库）、D5（分档）、
          D6（板-格换算收在一个函数里）、D7（三判据）、D16（单条 `aisles`）、
          D19（降级文案与四级走尽的出口）
          `tasks.md` 6.1（贪心主循环）、6.2（扣减不落库 + 确定性）、6.3（批次号）、
          7.2（回溯预测跨巷道）

## 本文件是唯一一处「谁拿到哪条巷」的决定点

调用链是单向的（D1）：`priority` 定序 → 本文件按序逐单决策，向下调 `reserved`（容量）/
`degradation`（分档与措辞）/ `scoring`（可行集与综合分）/ `factors`（因子取值）。反向的
依赖一处都没有：`scoring` 不知道队列，`priority` 不知道容量 —— 「资源仲裁与位置选择分离」
（`14` §一）因此是一件**结构**上的事，而不是注释里的一句话。

## 为什么队列序不在本文件里排第二遍

`order_queue` 已经给出全序 `(priority 降序, job_order_id 升序)`（D2 第 1 条），本文件
**按它遍历**。出参顺序就是落库的 `plans` 顺序（`17` §10.7 要求按 `priority` 降序），
两处各排一次的话，「A 类先挑」可以在容量结果里成立、在报文顺序里被另一套规则覆盖。

## 为什么扣减只在内存里，且**不**进因子取值

`AisleCap` 是这一版快照的**冻结**事实，分配不写库（D4）：本批已占的格数只记在
`_allocate_one` 的 `consumed` 计数里，交给 `feasible_aisles` 当判据 1 的减项。

但 **`cap` 因子的分子取的是快照的可用量，不减 `consumed`** —— 这一处不对称是刻意的：
`17` §10.1 有一条不变式「`scores` 可由取值复算」（7.1 的验证条件之一），而复算的人手里
只有快照与那一单，没有当时的 `consumed`。让因子值随竞争过程变化，落库的理由就成了只有
当事进程算得出的东西 —— 而推荐理由的全部意义是**事后能复核**。故：**扣减管可行不可行，
因子管好坏**，两者各管一段。

## 为什么「四级走尽」的边界是单而不是批

一张单分不出去不该让整批 500，也不该让它被静默塞进某条塞不下的巷道（`CLAUDE.md` §四
「绝不静默改写落位」）。故它进 `failures`、不占容量、同批其余单照常出方案 —— 每张单的
成败各自成说（`15` §11.1：停留 `PENDING`，可重试）。`degradation.allocation_failure`
给的是判断与措辞，处置（不迁状态、不回写批次号、不进 `plans`）是「不在 `outcomes` 里」
的自然结果，由 8.2 的状态迁移兑现。

## 每档只评「本档可行」的那些巷，而不是全部可行巷

链的语义是**先近后远**（`14` §3.5）：若把全部可行巷放在一起比总分，链就白设了 ——
总分最高的可能是一条远巷道。故逐档取「本档成员 ∩ 可行集」，第一个非空的档即停档，
`breakdown` / `scores` 的键集就是**这一档**的成员（`17` §10.1 的「候选巷道集」在这个
上下文里就是「本单参与比较的那些」）。更高档为什么没停下由 `tier_stop_reason` 写明。

`resolve_tiers` 的入参是**候选集**（`master ∩ caps`）、不是可行集：传可行集会让「本来没有
这类巷道」与「有但塞不下」两种空档含义合流，而两者的处置相反（补数据 / 腾容量，D19）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time

from app.core.enums import AbcClass
from app.engine.degradation import (
    AllocationFailure,
    Tier,
    allocation_failure,
    is_alarming,
    near_station_alert_message,
    resolve_tiers,
    tier_stop_reason,
)
from app.engine.factors import (
    FactorOutcome,
    InventoryProfile,
    SnapshotIndex,
    abc_factor,
    batch_factor,
    cap_factor,
    continuity_factor,
    effective_abc_class,
    existing_factor,
    require_order_cells,
    station_factor,
)
from app.engine.priority import QueueItem, order_queue
from app.engine.reserved import available_cap
from app.engine.scoring import (
    AisleState,
    feasible_aisles,
    score_aisles,
    select_best,
    station_degrade_reason,
)
from app.models.configuration import WEIGHT_FACTORS
from app.schemas.reason import DegradedAlert, FactorTerm, PredictedCrossAisle, PriorityPayload

__all__ = [
    "AllocationItem",
    "AllocationOutcome",
    "BatchAllocation",
    "allocate_batch",
    "predict_cross_aisles",
    "to_occupied_cells",
]


def to_occupied_cells(qty: int, *, cartons_per_pallet: int | None = None) -> int:
    """本单占用的**格数** —— 「板-格」换算的**唯一**落点（D6）。

    本阶段返回 `qty`（一单一位 = 一格 = 一板）。恒等不是占位符而是唯一与全部文档算例
    自洽的读法：`14` §2.2 用「板」作巷道容量的单位、§3.5 的告警文案也是板、`08` §14.1
    与 `CLAUDE.md` §十 的验收指标同样是板。真正的换算要 `JobOrder.qty`、`AisleCap` 的格、
    `Material.cartons_per_pallet` 三处单位都定了才谈得上（D14 登记在案）。

    **`cartons_per_pallet` 现在就收进签名**，虽然当前不影响结果：换算规则到齐时只改这一个
    函数体，而调用方**已经把材料参数递进来了** —— 若等到那时才加参数，每一处调用都要
    回头改，改一处漏一处不会有任何东西报错。
    """
    return qty


@dataclass(frozen=True)
class AllocationItem:
    """一条参与本批容量竞争的作业单（`14` §3.1 的队列输入 + 选道所需的两个字段）。

    字段分属两处消费：`job_order_id` / `order_abc_class` / `material_abc_class` /
    `outbound_qty` 供 `priority.QueueItem` 定序（`14` §3.2 的三项）；`material_code` /
    `order_batch_no` / `qty` 供因子取数与容量判据。**`existing_aisle_count` 不在其中** ——
    它由本文件从 `snapshot.profile(...).cross_aisle_count` 现取，不劳调用方再给一份：
    两份来源分叉时，「谁先挑」与「并回既有巷道」两件事会按不同的既有巷数算，而两边都跑
    得下去。

    `cartons_per_pallet` 是给 `to_occupied_cells` 的（D6：板-格换算的输入之一）。本阶段
    不给也不影响结果，但字段在这里 —— 与那个签名同一个理由。
    """

    job_order_id: str
    material_code: str
    qty: int
    #: 单据侧与物料侧的 ABC（`abc_factor` 的同一对入参：单据优先、物料兜底，`16` A.4）。
    order_abc_class: AbcClass | None = None
    material_abc_class: AbcClass | None = None
    #: 未来 N 天交货单出库量。`None` ⇒ 这份数据没拿到（触发整批排序降级，4.2）。
    outbound_qty: int | None = None
    order_batch_no: str | None = None
    cartons_per_pallet: int | None = None

    @property
    def abc_class(self) -> AbcClass | None:
        """**有效 ABC**：单据侧优先、物料侧兜底（同 `abc_factor` 与 `abc_term` 的规则）。

        预留池判据（`available_cap` 的 ABC 分支）与 `abc` 因子读的是同一个值 —— 两处若各
        写一遍兜底顺序，「物料是 A 类、单据未派生」的单会一边按 A 类取容量、一边按未知降级，
        而两边都不报错。
        """
        return effective_abc_class(self.order_abc_class, self.material_abc_class)

    def queue_item(self, *, existing_aisle_count: int) -> QueueItem:
        """转成定序侧的 `QueueItem`（`priority.py` 只认它，不认识容量与快照）。"""
        return QueueItem(
            job_order_id=self.job_order_id,
            order_abc_class=self.order_abc_class,
            material_abc_class=self.material_abc_class,
            outbound_qty=self.outbound_qty,
            existing_aisle_count=existing_aisle_count,
        )


@dataclass(frozen=True)
class AllocationOutcome:
    """一条**出了方案**的单：选中巷道 + 这份方案的完整证据（`17` §10.1 的原料）。

    证据与结论放在一起，是因为它们将被写进同一份 `payload_json`（7.1）：分两次取会让
    「按这一份算的分、拿那一份写的理由」留出余地，而 `score` 是个像模像样的浮点数。

    `scores` / `breakdown` 的键集 = **停档的成员**（不是全部候选，见模块 docstring）；
    `factor_degraded` 是**因子级**降级、`degraded` / `degrade_reason` 是**方案级**降级
    （走了降级链）—— 两者不共用字段（D8）。

    `alert` 挂在本单上而不是攒成一张表：告警是「这张单为什么被迫去了远巷道」的证据，
    与 `degrade_reason` 同源；按 `job_order_id` 事后配对会给错配留出余地。
    """

    item: AllocationItem
    priority: PriorityPayload
    #: 本次选中的推荐巷道集 —— **单条**；并列同分时全列并按 `aisle_no` 升序（D16）。
    aisles: tuple[str, ...]
    scores: Mapping[str, float]
    breakdown: Mapping[str, Mapping[str, FactorTerm]]
    factor_degraded: Mapping[str, str]
    #: 停在哪一档（档 0 = 近站台 ⇒ 没有降级）。结构化留档，供 8.x / 10.2 断言用。
    stop_tier: Tier
    degraded: bool
    degrade_reason: str | None
    alert: DegradedAlert | None = None


@dataclass(frozen=True)
class BatchAllocation:
    """一次批量分配的结果：出方案的单 + 四级走尽的失败单（`17` §10.7 的两条去向）。

    `outcomes` 按**队列序**（= `priority` 降序），即响应里 `plans` 的顺序，也是
    `degraded_alerts` 的顺序来源。`failures` 只存在于引擎结果里 —— `17` §10.7 的响应
    没有承载它的字段（D19 的未定项），端点侧因此只能表达「它不在 `plans` 里」。
    """

    outcomes: tuple[AllocationOutcome, ...] = ()
    failures: tuple[AllocationFailure, ...] = ()

    @property
    def alerts(self) -> tuple[DegradedAlert, ...]:
        """本批的 A 类降级告警（`17` §10.7 的 `degraded_alerts`），按队列序。"""
        return tuple(
            outcome.alert for outcome in self.outcomes if outcome.alert is not None
        )


def allocate_batch(
    *,
    items: Sequence[AllocationItem],
    weights: Mapping[str, float],
    state: AisleState,
    snapshot: SnapshotIndex,
    station_weights: Mapping[str, float],
    is_near_station: Mapping[str, bool | None],
    release_at: time,
    now: datetime,
) -> BatchAllocation:
    """本批的贪心分配（`14` §3.4）：定序 → 逐单选道 → 内存扣减 → 下一条。

    入参是**四个取数函数的产物**，全是只读的：`weights`（`scoring.load_weights`）、
    `state`（`load_aisle_state`）、`snapshot`（`factors.load_snapshot_index`）、
    `station_weights` / `is_near_station`（`factors.load_station_weights` / 巷道主数据行）。
    本函数**不碰会话**：整批只取一次数、之后在内存里跑完（D4）。快照缺失的阻断在批入口
    （8.x）—— 退化成「无候选巷道」会把一次「数据没到位」记成 N 次「单子分不出去」。

    `now` 是显式入参且**整批只取一次**（D2 第 3 条）：若循环里逐单取钟，同一批的单会
    按不同的钟点判「预留池释放了没有」，而「同样输入必得同样输出」也就不成立。

    空队列 ⇒ 空结果（`17` §10.7：空数组是明确的空集，不是「全量」）。
    """
    # 定序侧：既有巷道数由本文件从快照现取（`AllocationItem` 的 docstring：不留第二份来源）。
    ranked = order_queue(
        [
            item.queue_item(
                existing_aisle_count=snapshot.profile(
                    item.material_code
                ).cross_aisle_count
            )
            for item in items
        ]
    )
    # 造索引放在定序之后：`order_queue` 会为重复单据号当场报错，故到这里单号必然唯一
    # （静默覆盖会让一张单从所有方案里消失，而队列本身看不出问题）。
    by_id = {item.job_order_id: item for item in items}

    consumed: dict[str, int] = {}
    outcomes: list[AllocationOutcome] = []
    failures: list[AllocationFailure] = []
    for entry in ranked:
        decision = _allocate_one(
            by_id[entry.item.job_order_id],
            priority=entry.priority,
            weights=weights,
            state=state,
            snapshot=snapshot,
            station_weights=station_weights,
            is_near_station=is_near_station,
            release_at=release_at,
            now=now,
            consumed=consumed,
        )
        if isinstance(decision, AllocationFailure):
            failures.append(decision)
        else:
            outcomes.append(decision)
    return BatchAllocation(outcomes=tuple(outcomes), failures=tuple(failures))


def predict_cross_aisles(
    batch: BatchAllocation, *, snapshot: SnapshotIndex, threshold: int
) -> dict[str, PredictedCrossAisle]:
    """整批**回溯**：每个出了方案的料号，照此落位后会有几条巷道（`14` §3.4 步 5）。

    `| 既有快照中该物料占用的巷道 ∪ 本次分配给该物料的巷道 |`，`exceeded` 与
    `threshold` 一并给出（`17` §10.7）。**回溯**的时点是它的定义的一部分：逐单决策时
    看不见排在后面的同料号单，而「本次分配给该物料的巷道」这句话的主体是**料号**，
    不是单据 —— 故本函数收的是**整批**结果，且必须在 `allocate_batch` 跑完之后调。

    **键 = 出了方案的料号**，不是入参里的全部料号：四级走尽的单没有方案，预测也就无处
    可挂。多一个键会让调用方以为那张单有方案，少一个键则让 `plans` 里的某一条取不到
    预测 —— 两种都是「按料号取数」这条约定的失效形态。

    **既有集复用 `SnapshotIndex.profile().plates_by_aisle` 的键**，不另查一次库存：
    那几个键与三个库存因子同源（都过 `factors.aisle_of`），故 `[:2]` 这条口径只有一处。
    另写一条 `substr` 会让「因子按 `'01'` 聚、预测按别的聚」这种分叉悄无声息地成立
    —— 而两者都在报一个看起来正常的整数。

    **并集只取 `len()`**（D2 第 1 条：`set` 仅用于去重）：集合的迭代序由哈希决定，任何
    按遍历序取值的写法都会让同一份输入在两次运行里给出不同的数。

    **`threshold` 由调用方从 `CapacityConfig.same_material_cross_aisle_threshold` 取**
    （`16` §353~356 的默认值表里它是可改的口径），不在这里回落到 `Settings` 的引导值：
    现场改的是配置行，写死或另取一份会让「调小阈值」这件事在引擎里没有落点。

    **同批跨巷道不在这里算**（本期契约范围，`17` §10.7 的更正）：批号在分配时刻已知、
    同批分布本可聚合，不给是**范围**问题而不是做不到 —— 留待落位后验（`15` §1.3）。
    """
    # 按料号汇总本次分到的巷道。`outcomes` 是队列序（D2 第 1 条），故遍历序显式且稳定，
    # 返回字典的键序也跟着确定。
    assigned_by_material: dict[str, set[str]] = {}
    for outcome in batch.outcomes:
        assigned_by_material.setdefault(outcome.item.material_code, set()).update(
            outcome.aisles
        )

    predictions: dict[str, PredictedCrossAisle] = {}
    for material_code, assigned in assigned_by_material.items():
        existing = set(snapshot.profile(material_code).plates_by_aisle)
        cross_aisle_count = len(existing | assigned)
        predictions[material_code] = PredictedCrossAisle(
            material=cross_aisle_count,
            threshold=threshold,
            exceeded=cross_aisle_count > threshold,
        )
    return predictions


def _allocate_one(
    item: AllocationItem,
    *,
    priority: PriorityPayload,
    weights: Mapping[str, float],
    state: AisleState,
    snapshot: SnapshotIndex,
    station_weights: Mapping[str, float],
    is_near_station: Mapping[str, bool | None],
    release_at: time,
    now: datetime,
    consumed: dict[str, int],
) -> AllocationOutcome | AllocationFailure:
    """一条单的决策：分档 → 逐档取可行成员 → 本档评分选优 → 扣减（`14` §3.4）。

    `consumed` **就地改写**（键 = `aisle_no`，值 = 本批已占格数）：它是 D4 那份「进程内
    副本」的全部内容，本批结束即丢 —— 不落库、不回写 `AisleCap`。改的是传进来的那个字典
    而不是返回新字典，是为了让「扣减发生在哪一步」在调用点一眼可见（返回值只有一个）。
    """
    order_cells = to_occupied_cells(
        item.qty, cartons_per_pallet=item.cartons_per_pallet
    )
    require_order_cells(order_cells)
    profile = snapshot.profile(item.material_code)

    candidates = sorted(state.master & state.caps.keys())
    plan = resolve_tiers(candidates, is_near_station=is_near_station)
    feasible = set(
        feasible_aisles(
            state,
            abc_class=item.abc_class,
            order_cells=order_cells,
            release_at=release_at,
            now=now,
            consumed=consumed,
        )
    )

    for tier in plan.tiers:
        members = [aisle for aisle in tier.aisles if aisle in feasible]
        if members:
            stop_tier = tier
            break
    else:
        # 四级走尽：不是降级成功，是一条待人工处理的失败（D19）。
        return allocation_failure(job_order_id=item.job_order_id, plan=plan)

    breakdown, factor_degraded = _build_breakdown(
        members,
        item=item,
        order_cells=order_cells,
        state=state,
        profile=profile,
        station_weights=station_weights,
        release_at=release_at,
        now=now,
    )
    scores = score_aisles(
        weights=weights, breakdown=breakdown, degraded=tuple(factor_degraded)
    )
    best = select_best(scores)

    degrade_reason = tier_stop_reason(plan=plan, stop_tier=stop_tier)
    alert = _alert_for(
        item,
        plan=plan,
        stop_tier=stop_tier,
        order_cells=order_cells,
        best_aisle=best[0],
        state=state,
        release_at=release_at,
        now=now,
        consumed=consumed,
    )
    # 扣减在**告警之后**：缺口量说的是「这条单准备落位时近站台还剩多少」，含它自己刚占的
    # 那部分会让主管看到一个比现场小的缺口（虽然停档 ≥ 2 时这两者恰好相等 —— 停档 ≥ 2
    # 意味着本单没落在任何近站台巷道，故两条路径的结果相同；顺序在这里是为了不依赖那个
    # 巧合）。
    consumed[best[0]] = consumed.get(best[0], 0) + order_cells

    return AllocationOutcome(
        item=item,
        priority=priority,
        aisles=tuple(best),
        scores=scores,
        breakdown=breakdown,
        factor_degraded=factor_degraded,
        stop_tier=stop_tier,
        degraded=degrade_reason is not None,
        degrade_reason=degrade_reason,
        alert=alert,
    )


def _build_breakdown(
    aisles: Sequence[str],
    *,
    item: AllocationItem,
    order_cells: int,
    state: AisleState,
    profile: InventoryProfile,
    station_weights: Mapping[str, float],
    release_at: time,
    now: datetime,
) -> tuple[dict[str, dict[str, FactorTerm]], dict[str, str]]:
    """六因子 × 每条待评巷道 → `(breakdown, factor_degraded)`（`17` §10.1）。

    **降级是方案级的，故要两趟**：一趟收每条巷的六个取值、并把「有哪几个因子降级了」
    汇总起来（一条巷缺行 ⇒ 该因子在**全部**巷上一并降级 —— 否则各巷键集不相等，理由一
    落库就被 `schemas/reason.py` 的契约拒），第二趟才按汇总结果剔掉降级因子。逐巷各剔各的
    会让「这条巷有 `station`、那条没有」变成各巷键集不等。

    降级原因取**升序遍历里第一条**出事巷道的说法（`setdefault`），故与入参排列无关。

    `factor_degraded` 的键集恰为「六因子 − 参与评分的因子」这件事，由 `score_aisles` 在
    算分前再校验一道 —— 本函数里六因子的映射写在字面上（六个函数的签名各不相同，无法
    用元组驱动），漏掉一个的表现会是**分数算错**（分母跟着错），而那里当场报错。
    """
    abc_class = item.abc_class
    degraded: dict[str, str] = {}
    #: 每条巷的六个结果（未过滤）—— 降级要**先汇总再剔**，故中间态留全。
    outcomes_by_aisle: dict[str, dict[str, FactorOutcome]] = {}
    for aisle in aisles:
        cap = state.caps[aisle]
        outcomes = {
            "abc": abc_factor(
                order_abc_class=item.order_abc_class,
                material_abc_class=item.material_abc_class,
            ),
            # 分子取**快照**的可用量，不减本批已占（模块 docstring：`scores` 要可复算）。
            "cap": cap_factor(
                available=available_cap(
                    cap=cap, abc_class=abc_class, release_at=release_at, now=now
                ),
                cap_total=cap.cap_total,
            ),
            "existing": existing_factor(
                profile=profile, aisle=aisle, order_cells=order_cells
            ),
            "station": station_factor(distance_weight=station_weights.get(aisle)),
            "batch": batch_factor(
                profile=profile, aisle=aisle, order_batch_no=item.order_batch_no
            ),
            "continuity": continuity_factor(profile=profile, aisle=aisle),
        }
        for factor, outcome in outcomes.items():
            if outcome.is_degraded:
                degraded.setdefault(factor, outcome.degrade_reason)
        outcomes_by_aisle[aisle] = outcomes

    if "station" in degraded:
        # `station` 的成因措辞另有 3.3 的口径（整表未导出 / 部分导出点名哪几条），比逐巷
        # 的「无该巷道的站台主数据」更能说明去补哪份数据 —— 两者判据同源（该巷有没有
        # `AisleStation` 行），故这里覆盖不引入第二种判据。
        degraded["station"] = station_degrade_reason(
            aisles=aisles, station_weights=station_weights
        ) or degraded["station"]

    scored = [factor for factor in WEIGHT_FACTORS if factor not in degraded]
    # `scored` 已剔掉降级因子，而 `FactorOutcome` 保证「取值与降级原因恰有其一」（其
    # `__post_init__` 当场拦掉另一态），故取出的 `term` 必然非空 —— 这里的取值不是
    # 可能在可为空的字段上碰运气，而是两条不变式的推论。键集的完整性由 `score_aisles`
    # 在算分前校验（它当场报错，这里再查一遍只是把同一件事说两次）。
    breakdown = {
        aisle: {factor: outcomes[factor].term for factor in scored}
        for aisle, outcomes in outcomes_by_aisle.items()
    }
    return breakdown, degraded


def _alert_for(
    item: AllocationItem,
    *,
    plan,
    stop_tier: Tier,
    order_cells: int,
    best_aisle: str,
    state: AisleState,
    release_at: time,
    now: datetime,
    consumed: Mapping[str, int],
) -> DegradedAlert | None:
    """该单是否触发 A 类降级告警，触发则给出文案（`14` §3.5）。不触发 ⇒ `None`。

    `aisle` 取**扣减落点**（`best[0]`，并列时 `aisle_no` 最小者）而不是全列：`17` §10.7
    的告警只带一条巷道，而它要与 `plans[].aisles` 里「这一单实际会去哪儿」对得上
    （D16 的口径）。

    缺口量里的「近站台剩余可用」= 本单候选里那些近站台巷道的**当时**余额之和
    （逐条按 0 保底再相加：一条巷道的余额不可能为负，而负数会把缺口报大）。取候选集而不是
    全仓近站台巷道：这是「**这一单**能从近站台拿到多少」——`available_cap` 对 A 类与非 A 类
    给的数不同（预留池），同一时刻两条单的「剩余」本来就不是同一个数。
    """
    if not is_alarming(abc_class=item.abc_class, stop_tier=stop_tier):
        return None
    near_station_remaining = 0
    for aisle_no in plan.tiers[0].aisles:
        remaining = available_cap(
            cap=state.caps[aisle_no],
            abc_class=item.abc_class,
            release_at=release_at,
            now=now,
        ) - consumed.get(aisle_no, 0)
        near_station_remaining += max(0, remaining)
    return DegradedAlert(
        job_order_id=item.job_order_id,
        aisle=best_aisle,
        message=near_station_alert_message(
            plan=plan,
            order_cells=order_cells,
            near_station_remaining=near_station_remaining,
        ),
    )
