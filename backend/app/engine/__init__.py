"""核心推荐引擎（阶段三 / 设计 14）。

铁律：本地确定性计算 —— 同样输入必得同样输出。
禁止随机搜索、禁止大模型生成落位、禁止任何外部 LLM / API 调用。
资源仲裁与位置选择分离：批量分配决定「谁先挑」，6 因子评分决定「挑哪个巷道」。

## 这个包的公共面（`tasks.md` 1.2）

七个模块的公共入口在这里**再导出一次**，于是包外只需认一个入口：`from app.engine import
allocate_batch`。再导出只做搬运、**不做包装**（没有一层同名转发函数）—— 转发层会让
`grep` 到的定义点与真正的定义点错位，而本包的每个公共名字都带着「事实来源是文档哪一节」的
docstring，多一层就多一处会失真的副本。

`__all__` = 七个模块 `__all__` 的**并集**，外加 `WEIGHT_FACTORS`（见下）。测试断言的是
「逐项相等」而不是「包含」：漏掉一个名字的表现是 `ImportError`（看得见），而**多**一个
不该公开的名字（例如某个模块的内部 helper）不会有任何东西报错。

## 六因子集只有一个定义点

`WEIGHT_FACTORS` 直接取自 `app.models.configuration`（`WeightConfig` 的六列同源于它），
本包不再抄一份：抄一份的表现是两处各自演化，而因子集是 `14` §四 钉死「不增删」的东西 ——
真分叉时，评分算六项、契约校验却按五项的形态**看起来仍然正常**。本文件的 `__all__` 里
带着它，正是为了让「引擎的因子集是什么」在包入口即可读到（`17` §10.1 的列举顺序）。

## 依赖方向（`design.md` D1）

七个模块之间是一条单向图：`factors → scoring → allocator`，`priority` / `reserved` /
`degradation` / `reasons` 各自被上层取用。本文件**只 import、不被它们 import**（包内一律
写 `from app.engine.<module> import …`，不经过本文件）—— 否则就成了一条环，而环会让
「谁依赖谁」这句话失去意义。

## D14 的待业务方确认的默认值

`design.md` D14 的表格列了四项口径（另有一项「溢出区物理形态」与分档阈值共用落点）。
它们的**定义点在各自模块**——那是 D14「落地位置」列指定的家，也是「到齐后只改一处」这句话
的落点；本文件把四项**集中列一份**，并给**仍待确认**的三项各标一条 `TODO` 指向 D14 的表格行
（见文件末尾），让「哪些默认值还欠业务方一个数」有一个单一可读处（`grep TODO` 即得），
而不是散在三个模块的注释里。**N 已于 2026-09-12 确认（3 天，正本在 `14` §3.2），故不再挂
`TODO`** —— 表里保留该行是为了让四项的落地位置始终看得全；已确认项不再欠账，就不再出现在
待办清单里（清单欠账时是四条、现在是三条）。

| 项 | 本阶段的形态 | 定义点（D14 的「落地位置」） |
|---|---|---|
| 未来 N 天出库量的 **N** | `DEFAULT_OUTBOUND_WINDOW_DAYS = 3`（**已确认**，业务方 2026-09-12） | `priority.py` |
| 优先级三项权重系数 | `DEFAULT_PRIORITY_WEIGHTS`（等权） | `priority.py` |
| 板-格 / 箱-板换算规则 | 恒等（`to_occupied_cells()` 的函数体） | `allocator.py` |
| 「次近 / 远巷道」分档阈值 +「溢出区」形态 | 留空（`resolve_tiers()` 的档 1 / 档 3 恒为空集） | `degradation.py` |

四项都**不阻断**本阶段（D14 的结论）：每一项都只需要改一个点，不动分配器、不动端点、
不动契约。
"""
from __future__ import annotations

from app.engine.allocator import (
    AllocationItem,
    AllocationOutcome,
    BatchAllocation,
    allocate_batch,
    predict_cross_aisles,
    to_occupied_cells,
)
from app.engine.degradation import (
    DEGRADATION_TIER_LABELS,
    FAR_TIER_INDEX,
    AllocationFailure,
    Tier,
    TierPlan,
    allocation_failure,
    is_alarming,
    near_station_alert_message,
    near_station_gap,
    resolve_tiers,
    tier_stop_reason,
)
from app.engine.factors import (
    ABC_GRADE,
    ABC_GRADE_MAX,
    FactorOutcome,
    InventoryProfile,
    SnapshotIndex,
    abc_factor,
    aisle_of,
    batch_factor,
    cap_factor,
    continuity_factor,
    effective_abc_class,
    existing_factor,
    load_snapshot_index,
    load_station_weights,
    require_order_cells,
    station_factor,
)
from app.engine.priority import (
    ABC_ONLY_WEIGHTS,
    DEFAULT_OUTBOUND_WINDOW_DAYS,
    DEFAULT_PRIORITY_WEIGHTS,
    NO_ABC_WEIGHTS,
    QueueItem,
    RankedQueueItem,
    abc_term,
    build_priorities,
    existing_gain_term,
    order_queue,
    outbound_term,
    score_priority,
)
from app.engine.reasons import (
    BULK_BATCH_NO_PREFIX,
    build_reason_payload,
    load_bulk_batch_nos,
    next_bulk_batch_no,
)
from app.engine.reserved import available_cap, is_reserve_released
from app.engine.scoring import (
    AisleState,
    feasible_aisles,
    load_aisle_state,
    load_weights,
    score_aisles,
    select_best,
    station_degrade_reason,
)
from app.models.configuration import WEIGHT_FACTORS

#: 七个模块公共入口的并集 + `WEIGHT_FACTORS`（见模块 docstring「六因子集只有一个定义点」）。
#: 与各模块 `__all__` 的逐项关系由 `tests/logic/test_engine_package.py` 断言。
__all__ = [
    "ABC_GRADE",
    "ABC_GRADE_MAX",
    "ABC_ONLY_WEIGHTS",
    "BULK_BATCH_NO_PREFIX",
    "DEFAULT_OUTBOUND_WINDOW_DAYS",
    "DEFAULT_PRIORITY_WEIGHTS",
    "DEGRADATION_TIER_LABELS",
    "FAR_TIER_INDEX",
    "NO_ABC_WEIGHTS",
    "WEIGHT_FACTORS",
    "AllocationFailure",
    "AllocationItem",
    "AllocationOutcome",
    "AisleState",
    "BatchAllocation",
    "FactorOutcome",
    "InventoryProfile",
    "QueueItem",
    "RankedQueueItem",
    "SnapshotIndex",
    "Tier",
    "TierPlan",
    "abc_factor",
    "abc_term",
    "aisle_of",
    "allocate_batch",
    "allocation_failure",
    "available_cap",
    "batch_factor",
    "build_priorities",
    "build_reason_payload",
    "cap_factor",
    "continuity_factor",
    "effective_abc_class",
    "existing_factor",
    "existing_gain_term",
    "feasible_aisles",
    "is_alarming",
    "is_reserve_released",
    "load_aisle_state",
    "load_bulk_batch_nos",
    "load_snapshot_index",
    "load_station_weights",
    "load_weights",
    "near_station_alert_message",
    "near_station_gap",
    "next_bulk_batch_no",
    "order_queue",
    "outbound_term",
    "predict_cross_aisles",
    "require_order_cells",
    "resolve_tiers",
    "score_aisles",
    "score_priority",
    "select_best",
    "station_degrade_reason",
    "station_factor",
    "tier_stop_reason",
    "to_occupied_cells",
]

# --- D14 的待确认默认值：集中可读处，定义点在各自模块 -------------------------------------
# 形态与依据见模块 docstring 的 D14 表格；下面三条 TODO 是「还欠业务方一个数」的全量清单，
# 由 `tests/logic/test_engine_package.py` 断言恰好这三条。
#
# **这里刻意不再抄一遍可被 grep 命中的模式串**：注释里写出那个模式，搜索它的人就会把注释
# 本身也数进去（一次 `grep` 得 4 条而实数 3 条）—— 与 `scoring.py` 的 docstring 里写着
# 「引擎内部不调 `datetime.now()`」是同一种**自指**，那段代码正是为此才要先 `tokenize`
# 丢掉注释与字符串再搜。
#
# 第 1 行（N）已于 2026-09-12 确认（3 天，`14` §3.2）⇒ 本条不再欠账，故不挂 TODO。
# TODO(design.md D14 表格第 2 行)：优先级三项权重的系数 —— 改 priority.DEFAULT_PRIORITY_WEIGHTS
# TODO(design.md D14 表格第 3 行)：板-格 / 箱-板换算规则 —— 改 allocator.to_occupied_cells 的函数体
# TODO(design.md D14 表格第 4/5 行)：分档阈值与溢出区形态 —— 改 degradation.resolve_tiers 的解析
