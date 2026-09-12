"""降级链：近站台 → 次近巷道 → 远巷道 → 溢出区。每步记录降级原因（降级不静默）。`14` §3.5。

事实来源：`14` §3.5（链的名字与顺序；每步记录降级原因；A 类爆款被迫降级 ⇒ 告警 +
          「近站台缺口 X 板，建议移库腾挪」）、§3.4 步 4（可行集为空 → 进入降级链）
          `17` §2.1 / §3.4（`is_near_station` 存在且**可空**：NULL = 未导出）
          `design.md` D1（本模块只做「分档 + 措辞」，不选巷道、不算分）、
          D2 第 1 条（一切遍历序显式给出）、D5（分档由数据源的可表达性决定）
          `tasks.md` 5.1（分档解析器）、5.2（措辞与告警文案）、5.3（四级走尽）

本文件按任务书分三段落地，**三段已落齐**：**分档解析器**（5.1）→ **降级原因与
A 类告警文案**（5.2）→ **四级走尽的处置**（5.3）。

## 为什么「四级走尽」不叫降级

链的末端没有「再往下」的档：把「哪儿都放不下」记成一次降级，会让它在后验里与「降级到
远巷道」同形 —— 而后者是正常结果、前者是待人工处理的失败。故 5.3 出的是独立的
`AllocationFailure`（**不带巷道字段**：AC-005 的「不静默落位到非法巷道」由「没有巷道可
落」在结构上兑现），而不是 `DegradedAlert` 的又一档。

## 为什么是「解析器」而不是四个常量列表

档位成员是**数据**（这一版快照的 `is_near_station` 列），不是代码里的四个固定集合 ——
「谁算近站台」随快照冻结值变（`17` §3.4）。所以本模块给的是一个**函数**：吃进候选巷道与
那一列，吐出有序的分组；分配器拿它在自己那一段做逐档下探，本模块不持有任何快照状态。

## 为什么分档判据不是「距离值」

`14` §3.5 只给了档位的**名字**，没给阈值；能表达「次近巷道」的数据是
`AisleStation.distance_weight` 的**分档**，而分档阈值是业务口径、首期留空（D5：编造一个
阈值比留一个空档更糟）。故本阶段档 1 与档 3 是**空集但占位** —— 下探逻辑照常经过它们，
不需要为「首期只有两档」写第二条分支。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.enums import AbcClass

__all__ = [
    "DEGRADATION_TIER_LABELS",
    "FAR_TIER_INDEX",
    "AllocationFailure",
    "Tier",
    "TierPlan",
    "allocation_failure",
    "is_alarming",
    "near_station_alert_message",
    "near_station_gap",
    "resolve_tiers",
    "tier_stop_reason",
]

#: 四级降级链的标签，**顺序即优先级**（`14` §3.5）。档位下标 = 本元组的下标。
#:
#: 标签放在这里而不写进 `Tier` 的默认值：`degrade_reason` 要逐字写「停在哪一级」
#: （spec 的「逐级下探并记录原因」），文案与档位必须是同一份来源。
DEGRADATION_TIER_LABELS: tuple[str, str, str, str] = (
    "近站台",
    "次近巷道",
    "远巷道",
    "溢出区",
)


@dataclass(frozen=True)
class Tier:
    """降级链的一档：下标 + 标签 + 本档的巷道（显式序，见 `resolve_tiers`）。"""

    index: int
    label: str
    aisles: tuple[str, ...]


@dataclass(frozen=True)
class TierPlan:
    """一次分档的结果：四个有序分组 + 「近站台标志未导出」的那些巷道。

    `unknown_near_station` 与 `tiers[2].aisles` 是**交叉**的（前者是后者的子集）：未导出的
    巷道**已经**按档 2 参与下探（不被丢掉），这里再列一份是为了让 5.2 能在
    `degrade_reason` 里写出成因 —— 若它与 `False` 同形，操作员看到的会是一条没有成因的降级
    （`app/models/master_data.py` 第 3 条：NULL 不得当 `False` 用）。
    """

    tiers: tuple[Tier, ...]
    unknown_near_station: tuple[str, ...]


def resolve_tiers(
    aisles: Sequence[str], *, is_near_station: Mapping[str, bool | None]
) -> TierPlan:
    """把候选巷道分进四级链（`14` §3.5 / D5），返回有序分组。

    入参 `aisles` 是**判据 2/3 过关的候选巷道**（`master ∩ caps`），**不是**容量过滤后的
    可行集；`is_near_station` 是这一版快照的该列（键 = `aisle_no`，值可空）。本函数不判
    容量、不算分、不选巷道 —— 那些各有一处口径（`scoring.feasible_aisles` /
    `score_aisles`），容量判定由下探方**逐档**做（档内成员 ∩ 可行集）。

    **为什么不能传可行集**：那样每一档的成员都只剩「可塞下的那些」，空档于是有两个含义
    （本来没有这类巷道 / 有但塞不下），而这两种成因的处置完全不同（补数据 vs 腾容量，
    `tier_stop_reason` 要分开写）。取候选集时两者由 `tier.aisles` 是否为空区分得开。

    **成员判据**（D5 的表）：

    | 档 | 判据 | 首期 |
    |---|---|---|
    | 0 近站台 | `is_near_station is True` | 有数据时可达 |
    | 1 次近巷道 | 需 `distance_weight` 的分档阈值 | **空集**（阈值待业务口径） |
    | 2 远巷道 | `is_near_station is False` 或 **NULL（未导出）** | 有数据时可达 |
    | 3 溢出区 | 文档称最后一档，未定义物理形态 | **空集** |

    **NULL 为什么进档 2 而不是被丢掉**：丢掉等于一次静默的候选集收缩（本可落在这条巷的
    单被迫降级），而「降级不静默」要拦的正是这个。进档 2 表示「不享近站台优先」，同时由
    `unknown_near_station` 把它报出来（成因可写进理由）。映射里**没有**这条巷（取数侧漏了）
    走同一处置：两种形态在调用侧都是「这一列的权威值没拿到」。

    **档内按 `aisle_no` 文本升序**，与入参排列无关（D2 第 1 条）：档内次序会影响「得分并列
    时选谁」（D16 的并列裁决），最终落进落库的报文，故不能依赖 DB 返回序。
    """
    near: list[str] = []
    far: list[str] = []
    unknown: list[str] = []
    for aisle_no in sorted(aisles):
        flag = is_near_station.get(aisle_no)
        if flag is True:
            near.append(aisle_no)
        elif flag is False:
            far.append(aisle_no)
        else:
            # `None`（未导出）与「映射里没有这条巷」同处置，见 docstring。
            far.append(aisle_no)
            unknown.append(aisle_no)

    members: dict[int, list[str]] = {0: near, 1: [], 2: far, 3: []}
    return TierPlan(
        tiers=tuple(
            Tier(index=index, label=label, aisles=tuple(members[index]))
            for index, label in enumerate(DEGRADATION_TIER_LABELS)
        ),
        unknown_near_station=tuple(unknown),
    )


#: 「被迫降到远巷道」从哪一档算起 = 档 2（`14` §3.5 / spec 的「降级至远巷道或溢出区」）。
#: 用下标而不是标签比对：标签是给人看的文案，拿它当判据会在改文案时静默改掉告警面。
FAR_TIER_INDEX = 2


def tier_stop_reason(*, plan: TierPlan, stop_tier: Tier) -> str | None:
    """停在 `stop_tier` 的降级原因；停在档 0（近站台）⇒ `None`（没有降级）。

    两件事都要写明（spec 的「逐级下探并记录原因」）：**停在哪一级**、以及**上面几档为什么
    没停下**。而后者的成因要分开说 —— 某一档没有候选巷道（数据源问题：档 1 的阈值待定、
    或该快照/仓库本来就没有这类巷）与某一档有候选但塞不下（容量问题）处置不同：前者要补
    数据，后者要腾容量。

    未导出的近站台标志另起一句点名巷道号（D5 的补记）：只写「近站台无可行容量」会把人引去
    腾容量，而真问题是那一列没导出。**这一句是降级原因的一部分，不是告警的一部分** ——
    告警只发给 A 类爆款，而「标志未导出」影响的是**每一张**单。
    """
    if stop_tier.index == 0:
        return None
    skipped = "；".join(_tier_clause(tier) for tier in plan.tiers[: stop_tier.index])
    return f"逐档下探：{skipped}。停在「{stop_tier.label}」档{_unknown_note(plan)}"


def _tier_clause(tier: Tier) -> str:
    """一档的「为什么没停在这」：有成员却过不了容量判据 / 压根没有成员。

    两种成因的处置不同（补数据 vs 腾容量），故用同一句话区分 —— 措辞只写在这里，
    `tier_stop_reason`（停在第 N 档）与 `allocation_failure`（四级走尽）都用它。
    """
    return f"{tier.label}{'无可行容量' if tier.aisles else '无候选巷道'}"


def _unknown_note(plan: TierPlan) -> str:
    """未导出的近站台标志另起一句点名巷道号（D5 的补记），没有则返回空串。

    只写「近站台无可行容量」会把人引去腾容量，而真问题是那一列没导出。点名巷道号是让
    操作员知道去补哪几条的主数据。**降级原因与失败文案都要带这一句** —— 它是成因，
    不是告警（告警只发给 A 类爆款，而「标志未导出」影响每一张单）。
    """
    if not plan.unknown_near_station:
        return ""
    codes = "、".join(plan.unknown_near_station)
    return (
        f"（另有 {len(plan.unknown_near_station)} 条巷道的 is_near_station 未导出"
        f"（{codes}），已按远巷道处理 —— 补导出巷道主数据即可恢复近站台优先）"
    )


@dataclass(frozen=True)
class AllocationFailure:
    """四级走尽后该单的处置：**分配失败**、提示人工介入、状态停留 `PENDING`。

    字段只有「谁」与「为什么」（一段可直接展示的文案）；处置本身（不改状态、不写
    `bulk_batch_no`、不进 `plans`）由调用侧执行 —— 本模块只下判断与措辞（D1）。

    **没有巷道字段，这是刻意的**：AC-005 要的是「不静默落位到非法巷道」，而
    `DegradedAlert.aisle` 是必填的 `min_length=1`（`17` §10.7）—— 给失败单编一条巷道，
    就等于产出一条「落在某条不存在的巷道」的记录。它因此也**不是告警**：失败是「没有
    方案」，不是「方案降级了」。
    """

    job_order_id: str
    message: str


def allocation_failure(*, job_order_id: str, plan: TierPlan) -> AllocationFailure:
    """四级走尽 ⇒ 该单的失败记录（spec 的「四级走尽则该单失败而非静默落位」/ `15` §11.1）。

    这不是降级成功：链的末端没有「再往下」的档，把「哪儿都放不下」记成一次降级，会让它在
    后验里与「降级到远巷道」同形 —— 而后者是正常结果、前者是待人工处理的失败。

    文案要能被直接展示给操作员，故三件事都写出来：**四档都走过**（说明不是漏判）、
    **本单未产出方案且停留 `PENDING`**（说明它为什么不在 `plans` 里、以及可以重试）、
    **请人工介入**；成因（档位成员 vs 容量、标志未导出）沿用停档原因的同一措辞。
    """
    clauses = "；".join(_tier_clause(tier) for tier in plan.tiers)
    return AllocationFailure(
        job_order_id=job_order_id,
        message=(
            f"四级降级链走尽（{clauses}）{_unknown_note(plan)}——本单未产出方案，"
            "状态停留 PENDING，不出现在 plans 中、也不回写 bulk_batch_no，请人工介入"
        ),
    )


def near_station_gap(*, order_cells: int, near_station_remaining: int) -> int:
    """近站台缺口量 = 本单需量 − 近站台剩余可用，**不足为零**（`14` §3.5 的 X）。

    报差额而不是整单需量：主管据此决定是否移库腾挪，缺 2 板与缺 20 板的处置不一样；
    报整单会把这句指令变成「永远缺一整单」。单位沿用 `14` §3.5 的原文「板」——
    首期板-格换算取恒等（D6），两者是同一个数；换算规则到齐后此处随之改口径。

    `near_station_remaining < 0` 当场报错：它只会来自记账缺陷，而后果是**把缺口报大**
    （负剩余让差额虚增），一条发给主管的错数字没人复核得出来。
    """
    if near_station_remaining < 0:
        raise ValueError(
            f"近站台剩余可用不得为负（收到 {near_station_remaining}）——它按「可用 − 已占」"
            "取得，出现负数只能是扣减记账的缺陷，且会把缺口量报大"
        )
    return max(0, order_cells - near_station_remaining)


def near_station_alert_message(
    *, plan: TierPlan, order_cells: int, near_station_remaining: int
) -> str:
    """A 类爆款被迫降级的告警文案（`14` §3.5 / spec 的「A 类降级触发告警」）。

    `「近站台缺口 X 板，建议移库腾挪」` 这半句**逐字**照抄文档（spec 也逐字要求）：它是给
    主管的操作指令，同义句就不是同一句指令了。后面补的**成因**是本次实现加的，且是必需的
    —— 首期整表未导出时每条 A 类单都会命中这条告警（D5 的补记），文案若只说缺口，主管会
    去腾容量，而真问题是巷道主数据没导出（`16` §394 的预期状态）。三种成因分开说：
    近站台巷道无法识别（未导出）/ 本快照没有近站台巷道 / 有但容量不足。
    """
    gap = near_station_gap(
        order_cells=order_cells, near_station_remaining=near_station_remaining
    )
    message = f"近站台缺口 {gap} 板，建议移库腾挪"
    if not plan.tiers[0].aisles and plan.unknown_near_station:
        codes = "、".join(plan.unknown_near_station)
        message += (
            f"；成因：近站台巷道无法识别 —— {len(plan.unknown_near_station)} 条巷道的"
            f" is_near_station 未导出（{codes}），已按远巷道处理"
        )
    elif not plan.tiers[0].aisles:
        message += "；成因：本快照没有近站台巷道"
    else:
        message += "；成因：近站台可用容量不足"
    return message


def is_alarming(*, abc_class: AbcClass | None, stop_tier: Tier) -> bool:
    """该单停在 `stop_tier` 是否触发告警：**A 类** ∩ **停在档 2 / 档 3**。

    两个方向都必须收紧（`14` §3.5「若 **A 类爆款**被迫降级到**远巷道**」）：放宽到任何降级
    ⇒ 每条 B/C 单都告警，告警就不再是告警；放宽到任何单据 ⇒ 同上。`abc_class is None`
    **不告警** —— 未知档不是 A 类（`Material.abc_class`：「视为未知，不得当 C 类静默放行」，
    同理也不得当好料静默告警）。
    """
    return abc_class is AbcClass.A and stop_tier.index >= FAR_TIER_INDEX
