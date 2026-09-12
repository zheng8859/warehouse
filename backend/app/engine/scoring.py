"""单物料综合评分与可行巷道集求解。`14` §3.4。

事实来源：`14` §3.4（评分流程）、§3.1（队列输入取数）、§3.3（三列 cap 与预留比例）、
          §四（6 因子集合固定不得增删）
          `17` §2.1（巷道主数据）、§3.4（`AisleCap` 三列）、§七（配置实体：版本号 +
          生效时间 + 变更人）、§10.1（六项权重的示例值与可追溯恒等式）、§十一（数据隔离）
          `16` §353~356（预留比例 / 释放钟点 / N 等默认值表）
          `20` §六 `SC-004`（cap 已满的巷道不进入候选集）
          `openspec/changes/recommendation-engine/design.md` D1（版本语义三分）、
          D4（配置缺席的处置 + 容量扣减不落库）、D7（可行巷道集的三个判据）、
          D14（待确认默认值）、D17（`now` 是现场墙上时间）
          `tasks.md` 2.3（权重取数）、3.2（可行巷道集）、3.3（综合评分）

本文件有三段，按调用次序：**权重取数**（2.3，一批一次）→ **可行巷道集**（3.2，一单一次）
→ **综合评分与选道**（3.3，一单一次）—— 三段共享「本单 × 候选巷道」这个循环的上下文，
故不另开文件。

## 可行巷道集为什么不自己算「可用容量」

判据 1（`cap 足够`）与判据 3（非 A 类排除预留池）读的都是**同一个数**：`reserved.py` 的
`available_cap()`。本文件只负责「拿它与本单格数比、并把候选集定序」—— 若在这里再按
`cap_total` / `cap_reserved` 算一遍「可用」，池子的口径就有两份了，而两份分叉时两边
都跑得下去（D7 的公式只写在一处）。

## 权重缺席为什么是阻断，而容量缺席是取默认

这条不对称**不是引擎定的，是模型层已经做出的区分**，引擎只是兑现它（`design.md` D4）：

- `WeightConfig` 的六列**没有 Python 默认值** —— 权重是**打分口径**，缺席时取一套默认
  值会产出一个「看起来正常、但没人批准过」的排序，而它直接决定谁先挑黄金库位。
- `CapacityConfig` 的六项**都有列默认值**（`16` §353~356 的表值）—— 那是物理约束的
  保守默认，按表值跑与文档一致。

## 综合评分的三处口径为什么必须照抄 `17` §10.1

`14` §3.4 只说「用 6 因子评分排序」，量化细节全在 `17` §10.1（含一条算例）。三处都
**不是实现自选**，故各自有专门的用例，改动前先看那条：

1. **分母按参与评分的因子重新归一化** —— 满分恒为 `1.0`，跨轮次可比（有因子降级时也一样）。
2. **两位小数走十进制半值进位** —— `Decimal(str(x)).quantize(..., ROUND_HALF_UP)`，
   不是内置 `round()`（后者二进制 + 半值取偶，恰在半分位上与业务口径分叉）。
3. **比较用量化后值** —— 并列同分全列、按 `aisle_no` 升序。

## 为什么复用 `pick_current_version` 而不是自己写查询

「当前生效的是哪一版」= `effective_at <= now` 中 `version_no` 最大者（D1）。这条路
**只该有一套**：为什么是编号最大而不是生效时间最新、为什么不用指针列、为什么时间戳
必须朴素 —— 那些论证都在 `app/core/config_version.py` 的 docstring 里。这里再写一遍
查询，等于制造第二个口径，而两者分叉时没有任何东西会报错。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config_version import pick_current_version
from app.core.enums import AbcClass
from app.core.errors import BlockedMissingPrerequisite
from app.engine.factors import require_order_cells
from app.engine.reserved import available_cap
from app.models.configuration import WEIGHT_FACTORS, WeightConfig
from app.models.linkage import AisleCap
from app.models.master_data import Aisle
from app.schemas.reason import FactorTerm

__all__ = [
    "AisleState",
    "feasible_aisles",
    "load_aisle_state",
    "load_weights",
    "score_aisles",
    "select_best",
    "station_degrade_reason",
]


def load_weights(
    session: Session, *, warehouse_id: str, now: datetime
) -> dict[str, float]:
    """取该仓库**当前生效**的六项评分权重：`{因子名: 权重}`（`17` §10.1 的 `factors`）。

    键序按 `WEIGHT_FACTORS`（即 `17` §10.1 的列举顺序）—— `14` §四 钉死因子集不增删，
    而遍历序必须显式给出、不得依赖 DB 返回序（`design.md` 的红线落地第 1 条）。

    **无任何一版生效 ⇒ 抛 `BlockedMissingPrerequisite`**（阻断，不产出方案）。两类成因
    分开说，因为操作员的动作不同：一版都没配（去配一版）与配了但都还没到生效时间
    （等它生效，或改生效时间）。**不静默退到默认权重** —— 理由见模块 docstring。

    `now` 是显式入参（`design.md` 第 3 条）：引擎内部不调 `datetime.now()`，否则
    「同样输入必得同样输出」就不成立。**朴素时间**（naive）—— 列是朴素时间戳，
    传带时区的会由 `is_effective` 以一条指明改法的 `TypeError` 挡下。
    """
    rows = session.scalars(
        sa.select(WeightConfig).where(WeightConfig.warehouse_id == warehouse_id)
    ).all()
    current = pick_current_version(rows, now=now)
    if current is None:
        raise BlockedMissingPrerequisite(
            _missing_weights_message(
                warehouse_id=warehouse_id, now=now, configured=len(rows)
            ),
            detail={
                "warehouse_id": warehouse_id,
                "now": now.isoformat(),
                "weight_config_versions": len(rows),
            },
        )
    return {factor: getattr(current, f"weight_{factor}") for factor in WEIGHT_FACTORS}


def _missing_weights_message(*, warehouse_id: str, now: datetime, configured: int) -> str:
    """「无生效权重版本」的两种成因分开说 —— 配置行数就足以区分，不必再查一次。"""
    if configured == 0:
        return (
            f"评分权重配置未就绪（仓库 {warehouse_id} 一版都没有）—— 阻断本次分配，"
            f"不产出方案：不拿一套没人批准过的权重去排谁先挑黄金库位"
        )
    return (
        f"评分权重配置未生效（仓库 {warehouse_id} 已配 {configured} 版，但在 {now:%Y-%m-%d %H:%M} "
        f"无一版生效）—— 阻断本次分配，不产出方案：请确认生效时间是否还没到"
    )


@dataclass(frozen=True)
class AisleState:
    """一次批量分配的**巷道侧输入**：主数据（判据 2 的存在性）+ 快照容量（判据 1 / 3）。

    这就是 D4 说的「进程内的快照副本」：**一批只读一次**，分配器在整批里反复用它，并把
    「本批已占用多少」记在自己的计数里（`feasible_aisles` 的 `consumed`）。故本对象与其中
    的 `AisleCap` 行都**只读** —— 扣减不落库，`AisleCap` 仍是这一版快照的冻结值（D4）。

    两半分开存、不在这里取交集，是想让「这条巷为什么不在候选集里」有三种可分辨的成因：
    主数据没有它 / 这一版快照没有它 / 有它但容量不够。合成一个集合之后，前两种就分不开了，
    而它们的处置完全不同（前者是主数据没导，后者是这一版快照没算到）。
    """

    #: 本仓库巷道主数据的 `aisle_no` 集合（`17` §2.1）。
    master: frozenset[str]
    #: 该快照的 `AisleCap` 行，键 = `aisle_no`（`17` §3.4）。
    caps: Mapping[str, AisleCap]


def load_aisle_state(
    session: Session, *, warehouse_id: str, snapshot_id: int
) -> AisleState:
    """读一次本批要用的巷道侧输入（3.2 的取数侧）。

    `snapshot_id` **不接受 `None`**：快照缺失的处置是**阻断整批**（快照必须重新导入，
    `BlockedMissingPrerequisite`），那条路径在批入口（8.x），产出的是一条给操作员的提示。
    若在这里退化成「无候选巷道」，同一件事会变成「每张单四级走尽 → 分配失败」，把一次
    「数据没到位」记成 N 次「单子分不出去」—— 而后者看上去像引擎排不出来，指错了方向。

    两条过滤（`17` §十一 数据隔离）：`master` 按 `warehouse_id`，`caps` 再按 `snapshot_id`。
    `snapshot_id` 本身已隐含仓库，`caps` 上的仓库条件仍写上 —— `CLAUDE.md` §七 要求全部
    实体查询带仓库过滤，且它的代价只是一次索引前缀内的比较（`AisleCap` 的唯一键正是
    `(warehouse_id, snapshot_id, aisle_no)`）。
    """
    master = frozenset(
        session.scalars(
            sa.select(Aisle.aisle_no).where(Aisle.warehouse_id == warehouse_id)
        )
    )
    caps = {
        cap.aisle_no: cap
        for cap in session.scalars(
            sa.select(AisleCap).where(
                AisleCap.warehouse_id == warehouse_id,
                AisleCap.snapshot_id == snapshot_id,
            )
        )
    }
    return AisleState(master=master, caps=caps)


def feasible_aisles(
    state: AisleState,
    *,
    abc_class: AbcClass | None,
    order_cells: int,
    release_at: time,
    now: datetime,
    consumed: Mapping[str, int] | None = None,
) -> list[str]:
    """本单的**可行巷道集**：D7 的三判据过筛后，按 `aisle_no` 升序返回。

    | 判据（`14` §3.4 步 1） | 在这里的形态 |
    |---|---|
    | `巷道可用` | `state.master` 与 `state.caps` 的交集 |
    | 非 A 类排除预留池 | 交给 `available_cap` 的 ABC 分支（本文件不重算） |
    | `cap 足够` | `available_cap(...) - consumed >= order_cells` |

    **遍历序 = `aisle_no` 升序，且与 DB 返回序无关。** 升序按**文本**：`aisle_no` 是两位
    零填充文本（`aisle_no_len2` 钉了长度），故文本序与数值序一致。不显式排序的后果不是
    报错，而是「换个查询计划就换了推荐次序」—— 候选集的次序会逐项落进 `plans`，而它是
    落库的报文（`design.md` 红线落地第 1 条：一切遍历序显式给出）。

    **`consumed` 是同一批里前面几张单已占的格数**（键 = `aisle_no`，未提及即 0），也就是
    D4 那份「进程内副本」的当前值。它在这里而不是在分配器里减：判据 1 只有一个写法，
    分配器只负责记账（`{aisle_no: 已占格数}`）。**扣减量按整条巷道的可用量减，不拆
    「先吃预留池还是先吃可用部分」** —— 本阶段没有那个口径（`14` §3.4 步 3 只写「扣减该
    巷道容量」），取的是**偏保守**的读法：少给不会破「非 A 类不占预留池」那条红线，
    多给会。理由与取舍的完整论证见 D4 的补记。

    **可行集为空不是异常，是一条结果**：该单走「四级走尽 → 分配失败」，停在 `PENDING`
    等人工介入（D5 / `15` §11.1）。抛异常会让整批 500，而每张单的成败本就该各自成说。

    `order_cells <= 0` 直接 `ValueError`（唯一判据在 `factors.require_order_cells`）：
    否则 `可用 >= 0` 恒真，一张 0 格的单会拿到一份看起来很正常的推荐。
    """
    require_order_cells(order_cells)
    already_used = consumed or {}
    feasible: list[str] = []
    for aisle_no in sorted(state.master & state.caps.keys()):
        remaining = available_cap(
            cap=state.caps[aisle_no],
            abc_class=abc_class,
            release_at=release_at,
            now=now,
        ) - already_used.get(aisle_no, 0)
        if remaining >= order_cells:
            feasible.append(aisle_no)
    return feasible


def station_degrade_reason(
    *, aisles: Collection[str], station_weights: Mapping[str, float]
) -> str | None:
    """`station` 因子是否**方案级降级**：返回降级原因，`None` = 该因子照常参与。

    判据是「这批候选巷里有没有哪条没有 `AisleStation` 行」，两条口径见 `factors.py` 模块
    文档第 4 条与 2.2 的实施补记：一条候选巷缺行 ⇒ 该因子在**全部**候选上一并降级，因为
    `17` §10.1 要求每条巷的 `breakdown` 键集都恰为「六因子 − `factor_degraded`」，部分参与
    会让各巷键集互不相等（`schemas/reason.py` 的 `_contract_invariants` 正是那条检查）。

    **逐单求，不是整批求一次**：候选集随容量扣减而变，同一批里前后两张单的候选集可以不同，
    降级结论也就跟着不同 —— 这正是本判据不能落在单因子函数（只有一条巷的视野）里、
    要等 `scoring.py` 拿到候选集才谈得上的原因。

    两种成因分开说：**整表未导出**（首期形态，`16` §394 / `A.4`）用 `17` §10.1 的示例措辞；
    **部分导出**则点名是哪条巷缺行 —— 后者是可修的（补导出即可），前者不是。
    """
    missing = sorted(aisle for aisle in aisles if aisle not in station_weights)
    if not missing:
        return None
    if not station_weights:
        return "巷道-站台主数据未导出"
    return (
        f"候选巷道 {'、'.join(missing)} 无站台主数据（AisleStation）——"
        f"该因子在全部候选上一并降级"
    )


def score_aisles(
    *,
    weights: Mapping[str, float],
    breakdown: Mapping[str, Mapping[str, FactorTerm]],
    degraded: Collection[str] = (),
) -> dict[str, float]:
    """`scores`：每巷总分 = **可用因子**加权和 ÷ 可用因子权重之和，两位小数（`17` §10.1）。

    - **分母按参与评分的因子重新归一化**，故六因子齐全时为 `1.0`、有因子降级时满分仍是
      `1.0`（跨轮次可比）。降级因子的权重**不进分母**。
    - **求和序固定按 `WEIGHT_FACTORS`**，不按调用方给的映射序：浮点加法不满足结合律，
      次序不同会让末位漂移 —— 而输出是两位小数的量化值，漂移只在恰好落在半值时可见，
      正是最难查的那种差异（`17` §10.1 已就半分位专门提醒）。
    - **两位小数走十进制半值进位**（`Decimal(str(x)).quantize(..., ROUND_HALF_UP)`），
      不是内置 `round()`：后者走二进制且半值取偶，在恰好半个分位上与业务口径分叉
      （`17` §10.1 明令，并点名了这一处）。
    - **键序 = `aisle_no` 升序**：与 `feasible_aisles` 的输出同序，也是 `17` §10.1 算例里
      `breakdown` / `scores` 的排列（一切遍历序显式给出）。

    `degraded` 是**方案级**的可用因子剔除（来自 `station_degrade_reason` 与各因子函数的
    降级结论），本函数据它校验每条巷的分解键集恰为「六因子 − 降级因子」。**校验放在算分
    之前**：键集错了的表现不是报错而是**分数算错**（分母跟着错），那时已经晚了。同一份
    不变式在 `schemas/reason.py` 里还有一道（落库前），两处的时机不同、都要有。
    """
    unavailable = set(degraded)
    scored_factors = [factor for factor in WEIGHT_FACTORS if factor not in unavailable]
    denominator = sum(Decimal(str(weights[factor])) for factor in scored_factors)
    if denominator == 0:
        raise ValueError(
            f"可用因子的权重和为 0（参与评分的是 {scored_factors}）——无从归一化；"
            "六项权重全配 0 是配置问题（模型只要求六列存在），请检查 WeightConfig 现值"
        )

    scores: dict[str, float] = {}
    for aisle in sorted(breakdown):
        terms = breakdown[aisle]
        if set(terms) != set(scored_factors):
            raise ValueError(
                f"巷道 {aisle} 的分解键集必须恰为参与评分的因子"
                f"（缺 {sorted(set(scored_factors) - set(terms))}、"
                f"多 {sorted(set(terms) - set(scored_factors))}）——"
                "这条不变式来自 17 §10.1（每巷键集 = 六因子 − factor_degraded）"
            )
        numerator = sum(
            Decimal(str(weights[factor])) * Decimal(str(terms[factor].value))
            for factor in scored_factors
        )
        scores[aisle] = float(
            (numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    return scores


def select_best(scores: Mapping[str, float]) -> list[str]:
    """得分最高的巷道 —— 并列同分则**全列**，按 `aisle_no` 升序（`17` §10.1 / D16）。

    比较用的是 `score_aisles` 交出来的**量化后**值（「比较用量化后值」）：两条巷的原始加权
    和若只差在小数点后第六位，量化后同分即并列，不该让浮点末位决定谁进 `aisles`。

    并列时**扣减落在 `aisle_no` 最小的一条**（`17` §10.1 的字段语义）—— 那一步在分配器里
    （它才知道要扣哪条巷的容量），故本函数只给出并列的全集、并按 `aisle_no` 升序给出，
    让「最小者」天然是第一个。

    可行集为空 ⇒ `ValueError`：那一步该走**降级链**（`14` §3.4 步 4）。静默返回 `[]` 会让
    「已经没有巷道可放」与「选出来是空的」在调用点长得一样。
    """
    if not scores:
        raise ValueError(
            "可行巷道集为空时不该走到选道 —— 该走降级链（14 §3.4 步 4：可行集为空 → 下探）"
        )
    best = max(scores.values())
    return sorted(aisle for aisle, score in scores.items() if score == best)
