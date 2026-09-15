"""6 因子的单因子取值：`abc` / `cap` / `existing` / `station` / `batch` / `continuity`。

因子固定不得增删（`14` §四），六个都在本文件。`station` 与其余五个**不同源**：它读
`AisleStation` 主数据，其余读库存快照 —— 但两者都是「一个巷道上的一个取值 + 取值说明」，
故同置一处，取数各自成对（`load_snapshot_index` / `load_station_weights`）。

事实来源：`14` §3.1（队列输入的取数）、§3.2（等级分 A=3 / B=2 / C=1）、§3.3（cap 三列）、
          §3.4（集中度）
          `17` §2.2（巷道-站台主数据）、§3.3（库存分布）、§3.4（cap 三列）、
          §10.1（**归一化口径的唯一反推依据**）
          `16` §171（导入期「数量 > 0」的口径异常）、§394 / `A.4`（首期无真实数据的预期形态）
          `20` §六 `SC-003`（批次因子读库存快照的既有批号集）/ `SC-005`（既有同物料抬分）
          `design.md` D16（六因子的归一化口径表）、`tasks.md` 2.1 / 2.2（本文件的任务书）

## 本文件的边界：只管「一个巷道上的一个因子」

一次调用得**一个取值**（带取值说明）或**一条降级原因**，不做跨巷道比较、不读权重、
不碰容量扣减。队列排序在 `priority.py`、加权归一化在 `scoring.py`、可行集与选道在
`allocator.py` —— 权重与队列上下文只属于它们。故本文件对同一条巷道可以被反复调用，
而结果只取决于入参与快照。

## 四处口径不是文档直给的，依据在这里

1. **`cap_total == 0` 不降级，给 0.00。** 该列按 `17` §3.4 已是「总格数 − 已占格数」的
   **净额**，为 0 是「满巷道」这一合法状态，不是数据缺失。而因子级降级是**方案级**的
   （`17` §10.1 要求每条巷道的 `breakdown` 键集都等于「六因子 − 降级因子」），为一条
   巷道的满仓去降级，等于把一条巷道的事实放大成整批的事实 —— 且每条巷道都会被同一个
   缺失剔掉，连带 `cap` 这个因子对全部候选失效。
2. **`existing` 的分母是「本单占用格数」，为 0 时 `ValueError`。** `16` §171 把「数量 > 0」
   列为导入期必须阻断的口径异常，所以 0 格的单不该走到评分这一步。静默给 0 会让
   「分母写错」与「本单真的没有集中度」两种成因长得一样（后者是合法的 0.00）。
   判据落在公共函数 `require_order_cells` —— `scoring.feasible_aisles`（3.2）的判据 1
   也要它（否则 `可用 >= 0` 恒真），两处共用一份文案与依据。
3. **本单批号为空 ⇒ 降级（2026-09-11 更正）。** 批号由系统在**入库单建立时按生产批规则
   生成**（同一生产批共用同一批号），分配时刻**已知**，故 `batch` 是正常参与的因子：
   比对「本单批号 ∈ 该巷既有批号集」，既有集读**库存快照**（`SC-003`）。`None` 于是只剩
   「登记侧该生成而没生成」这一种成色 —— 仍降级而不是记 0.00：后者会把「无从比较」记成
   「比过了、没有」，而只有前者该出现在 `factor_degraded` 里（`17` §10.1「两种降级不得
   混用」的同一精神）。
4. **`station` 的判据是「该巷有没有 `AisleStation` 行」，且它是方案级的。** 一条候选巷缺行
   ⇒ 该因子在**全部**候选上一并降级：`17` §10.1 要求每条巷道的 `breakdown` 键集都恰为
   「六因子 − `factor_degraded`」，部分参与会让各巷键集互不相等，理由一落库就被契约拒
   （`schemas/reason.py` 的 `_contract_invariants` 正是那条检查）。首期该表为空 ⇒
   **恒降级**是这条判据的推论，不是硬编码（`16` §394 的预期状态）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import AbcClass
from app.models.linkage import InventoryItem, Snapshot
from app.models.master_data import AisleStation
from app.schemas.reason import FactorTerm

__all__ = [
    "ABC_GRADE",
    "ABC_GRADE_MAX",
    "FactorOutcome",
    "InventoryProfile",
    "SnapshotIndex",
    "abc_factor",
    "aisle_of",
    "batch_factor",
    "cap_factor",
    "continuity_factor",
    "effective_abc_class",
    "existing_factor",
    "load_snapshot_index",
    "load_station_weights",
    "require_order_cells",
    "station_factor",
]

#: 等级分（`14` §3.2：A=3 / B=2 / C=1），归一化的分母即最大值 3。
#: 取值本身写在文档里，这里只把它落成一处 —— `abc` 因子与 `priority` 的 ABC 分量
#: （`17` §10.1 示例里同为 `A → 1.00`）用的是同一套分数，两处各写一份就会分叉。
ABC_GRADE: Mapping[AbcClass, int] = {AbcClass.A: 3, AbcClass.B: 2, AbcClass.C: 1}

#: 归一化分母 = 等级分上界。写成常量而不是 `len(ABC_GRADE)`：后者在「补一个 D 类」时
#: 会静默把 A 类从 1.00 变成 0.75，而 `14` §3.2 的三档是固定的。
ABC_GRADE_MAX = 3

#: 快照缺失时的降级原因。`InventoryProfile.degrade_reason` 正常由造数/调用侧给出，
#: 这里只是兜底 —— 降级必须有话说（「降级不静默」）。
_NO_SNAPSHOT_REASON = "库存快照缺失（未导入或已过期）——该因子无从取数"


def _as_member(abc_class: AbcClass | str) -> AbcClass:
    """归一成枚举成员。收 `AbcClass` 也收它的字符串值（`str` 枚举在库里同列同值）；
    给出枚举外的取值时**当场报错** —— `17` §九 的枚举不得增删，静默落到「当 C 类」
    正是那个口径要拦的事（`Material.abc_class` 的注释：「视为未知，不得当 C 类放行」）。

    说明文字一律取 `.value` 而不是直接格式化成员：`str` 枚举的 `__format__` 在
    3.12 前后给出的串不同（`AbcClass.A` vs `A`），而这条说明是给操作员看的，
    不该随解释器版本变。
    """
    return AbcClass(abc_class)


@dataclass(frozen=True)
class FactorOutcome:
    """**一条巷道上的一个因子**的结果：有取值（带取值说明）或降级（带原因），恰有其一。

    两个字段都判空（或都给）的结果没法被消费方解读：`scoring.py` 要按它决定这个因子是否
    进分母，`reasons.py` 要按它决定写进 `breakdown` 还是 `factor_degraded`（`17` §10.1）。
    所以这条不自洽在**构造时**就拦掉，而不是等某条分支拿到一个 `term=None` 的 `scored`。

    取值与原因都**不是**异常：因子级降级按 `14` §3.5 不阻断作业，是正常返回值的一种。
    """

    term: FactorTerm | None = None
    degrade_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.term is None) == (self.degrade_reason is None):
            raise ValueError(
                "FactorOutcome 必须恰有其一：term（取值 + 取值说明）或 degrade_reason"
            )
        if self.degrade_reason is not None and not self.degrade_reason.strip():
            raise ValueError("降级必须写明原因（降级不静默）")

    @property
    def is_degraded(self) -> bool:
        return self.term is None

    @classmethod
    def scored(cls, value: float, note: str) -> FactorOutcome:
        """参与评分。`note` 用**原始口径**（「可用 58 / 80 板」而不是「0.72」）——
        `22` §三 的因子行要让操作员看懂「为什么是这个分」。"""
        return cls(term=FactorTerm(value=value, note=note))

    @classmethod
    def degraded(cls, reason: str) -> FactorOutcome:
        """降级不参与评分。原因要写到操作员能判断「该去补哪份数据」的程度。"""
        return cls(degrade_reason=reason)


@dataclass(frozen=True)
class InventoryProfile:
    """**一个料号**在一份快照里的分布 —— 三个读库存的因子的共同取数。

    按料号切开是刻意的：`existing` 与 `continuity` 都是「**同物料**」的口径，串号会让
    A 料的库存为 B 料抬分，而且抬得看不出来（`17` §3.3 的库存行本就按料号聚合使用）。

    空档案（`snapshot_present=True` 而三张表皆空）是**合法**的：该料号在库里没有库存，
    故 `existing` / `continuity` 取 0.00。这与「没有快照」严格不同，后者降级。
    """

    #: 快照是否存在。`False` ⇒ 三个因子一律降级，`snapshot_present=True` 时下面的三项才可信。
    snapshot_present: bool

    #: 巷道 → 板数。按 `location_code[:2]` 聚合（见 `aisle_of`）。
    plates_by_aisle: Mapping[str, int] = field(default_factory=dict)
    #: 巷道 → 该巷的既有批号集。`existing` 数板数、`batch` 比批号，同一个巷道的两种事实。
    batches_by_aisle: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: 批号 → 巷道 → 数量。`plates_by_aisle` 只有巷道总量，出库顺路取要按批 FIFO、按巷拆量
    #: （`derive_pick_sequence`），故把「同一批号在哪些巷各有多少」单独存一份。
    qty_by_batch_by_aisle: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    #: 快照缺失时的原因（`snapshot_present=False` 时才有意义）。
    degrade_reason: str | None = None

    @property
    def aisles(self) -> frozenset[str]:
        """该料号当前占用的巷道集（板数 > 0 的巷道）。`continuity` 的分母来源。"""
        return frozenset(self.plates_by_aisle)

    @property
    def cross_aisle_count(self) -> int:
        """同物料跨巷道数 —— **分配前**的既有值。分配后的预测值在 `17` §10.7 的
        `predicted_cross_aisle` 里，由分配器回溯算（那时才含本次分配到的巷道）。"""
        return len(self.aisles)


@dataclass(frozen=True)
class SnapshotIndex:
    """一份快照按**料号**切好的索引 —— 一次取数、多次查询。

    队列里每条单都要查自己的料号，逐单去 SELECT 会把同一份快照读 N 遍；而分配器在
    纯内存里跑（`design.md` D7 的扣减不落库），取数就该是一次性的。故索引是**只读**
    的取数结果：造一个、传给所有因子，因子自己不再碰会话。
    """

    snapshot_present: bool
    #: 料号 → 巷道 → 板数 / 批号集。两层映射而不是「一条料号的档案」列表：分配时按料号
    #: 直取，没有线性查找，也没有「同一料号出现两次」这种要合并的中间态。
    plates_by_material: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    batches_by_material: Mapping[str, Mapping[str, frozenset[str]]] = field(
        default_factory=dict
    )
    #: 料号 → 批号 → 巷道 → 数量。出库顺路取要按批 FIFO 拆巷，`plates_by_material` 只给
    #: 巷道总量（分配器的既有集仍用它），这份三层映射供 `derive_pick_sequence` 取数。
    qty_by_batch_by_material: Mapping[str, Mapping[str, Mapping[str, int]]] = field(
        default_factory=dict
    )
    degrade_reason: str | None = None

    @classmethod
    def absent(cls, reason: str) -> SnapshotIndex:
        """无快照可用时的索引 —— 三个读库存的因子据它降级，**不阻断**分配（`14` §3.5）。"""
        return cls(snapshot_present=False, degrade_reason=reason)

    @classmethod
    def build(cls, rows: Iterable[InventoryItem]) -> SnapshotIndex:
        """把**一份快照**的库存行聚成索引。

        调用方负责只喂这一份快照的行 —— 索引无从分辨行来自哪一版（`17` §3.2 的旧版
        快照归档不删除，若把历史版本也喂进来，集中度会被历次导入反复加权）。
        """
        plates: dict[str, dict[str, int]] = {}
        batches: dict[str, dict[str, set[str]]] = {}
        qty_by_batch: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            aisle = aisle_of(row.location_code)
            material_plates = plates.setdefault(row.material_code, {})
            material_plates[aisle] = material_plates.get(aisle, 0) + row.qty
            batches.setdefault(row.material_code, {}).setdefault(aisle, set()).add(
                row.batch_no
            )
            qty_by_batch.setdefault(row.material_code, {}).setdefault(
                row.batch_no, {}
            ).setdefault(aisle, 0)
            qty_by_batch[row.material_code][row.batch_no][aisle] += row.qty
        return cls(
            snapshot_present=True,
            plates_by_material={m: dict(v) for m, v in plates.items()},
            batches_by_material={
                m: {a: frozenset(b) for a, b in v.items()} for m, v in batches.items()
            },
            qty_by_batch_by_material={
                m: {b: dict(a) for b, a in by_batch.items()}
                for m, by_batch in qty_by_batch.items()
            },
        )

    def profile(self, material_code: str) -> InventoryProfile:
        """取一个料号的分布。没有库存的料号返回**空档案**（合法），不是降级。"""
        if not self.snapshot_present:
            return InventoryProfile(
                snapshot_present=False, degrade_reason=self.degrade_reason
            )
        return InventoryProfile(
            snapshot_present=True,
            plates_by_aisle=dict(self.plates_by_material.get(material_code, {})),
            batches_by_aisle=dict(self.batches_by_material.get(material_code, {})),
            qty_by_batch_by_aisle=dict(
                self.qty_by_batch_by_material.get(material_code, {})
            ),
        )


def aisle_of(location_code: str) -> str:
    """库位号 → 巷道：**按文本切片 `[:2]`，不得数值化。**

    `CLAUDE.md` §七：Excel 会把 `010104` 数值化成 `10104`，前导 0 一丢巷道就错了 ——
    而错一条巷道会同时错掉 `existing` / `batch` / `continuity` 三个因子，且现场看不出异常
    （分数照样给，只是给错了巷道）。6 位是库位号的既定长度（`17` §3.3 的 CHECK）。
    """
    return location_code[:2]


def load_snapshot_index(session: Session, *, snapshot_id: int | None) -> SnapshotIndex:
    """读**指定快照**的库存分布，聚成索引。

    `snapshot_id` 为 `None` 即「当前无快照」——调用方（分配器）在取当天快照时可能拿到
    空，这时不该缺省成「读全部库存行」：那会把历次导入叠成一份不存在的现场。
    快照 id 指向一行不存在的记录同样降级：`17` §3.2 的旧版会被归档，一个失效的 id
    与「没有快照」在取数上是一回事。

    **不按 `warehouse_id` 过滤**：快照行本身已归属一个仓库（`17` §11 的隔离在快照
    这一层成立），库存行按 `snapshot_id` 取即已被它限定。
    """
    if snapshot_id is None:
        return SnapshotIndex.absent("当前无库存快照（未导入 INV 文件）——该因子无从取数")
    if session.get(Snapshot, snapshot_id) is None:
        return SnapshotIndex.absent(f"快照 {snapshot_id} 不存在——该因子无从取数")
    rows = session.scalars(
        sa.select(InventoryItem).where(InventoryItem.snapshot_id == snapshot_id)
    )
    return SnapshotIndex.build(rows)


def load_station_weights(session: Session, *, warehouse_id: str) -> dict[str, float]:
    """读该仓库的**巷道-站台主数据**：巷道号 → `distance_weight`（`17` §2.2）。

    与 `load_snapshot_index` 的差别不只是源表：这里**没有「版本」的概念** —— 主数据是
    当前态，而库存是快照态。故返回裸字典而不是一个索引类型：调用方要问的问题只有一个
    「这一巷有没有」，`dict.get` 已经是那个问题的最短答案。

    **首期返回空字典**是预期状态（`16` §394 / `A.4`：该表待业务方补充导出）。调用方
    不能把「空」当成「没有站台数据所以随便落位」—— 空 ⇒ `station` 因子降级，理由要写进
    推荐理由（`17` §10.1 的示例原因即「巷道-站台主数据未导出」）。

    按 `warehouse_id` 过滤：与快照不同，主数据行**没有归属快照**，一个仓库一份当前态，
    不过滤就会把别的仓库的巷道权重读到本仓的巷道上（`17` §11 的数据隔离）。
    """
    rows = session.execute(
        sa.select(AisleStation.aisle_no, AisleStation.distance_weight).where(
            AisleStation.warehouse_id == warehouse_id
        )
    )
    return {aisle_no: weight for aisle_no, weight in rows}


def _inventory_degrade(profile: InventoryProfile) -> FactorOutcome:
    """快照缺失 → 降级。`degrade_reason` 正常由取数侧写明（`SnapshotIndex.absent`），
    这里只兜底一个「有话说」的原因。"""
    return FactorOutcome.degraded(profile.degrade_reason or _NO_SNAPSHOT_REASON)


def effective_abc_class(
    order_abc_class: AbcClass | None, material_abc_class: AbcClass | None
) -> AbcClass | None:
    """**有效 ABC**：单据侧优先、物料侧兜底（`16` A.4）—— 这条规则唯一的一处落点。

    单据上的 ABC 是入队时抄下来的当次事实，物料主数据上的是成品清单聚合的结果；两者
    不一致时以单据为准。两个消费方（`abc` 因子的取值、`available_cap` 的预留池分支）读
    的必须是同一个值：各写一遍兜底顺序，会让「物料是 A 类、单据未派生」的单一边按 A 类
    取容量、一边按未知降级，而两边都不报错。

    `None`（两侧皆空）= 未知档，**不是** C 类：调用侧的处置各不相同（因子降级 / 非 A 类
    容量 / 不告警），但都不是「当成最低档放行」。
    """
    return order_abc_class if order_abc_class is not None else material_abc_class


def abc_factor(
    *, order_abc_class: AbcClass | None, material_abc_class: AbcClass | None
) -> FactorOutcome:
    """`abc`：等级分 ÷ 3。**与巷道无关** —— 同一队列项对所有候选取同一个值，故它不改

    排序，只在加权时把「A 类更该靠近站台」这件事计入（`14` §3.2）。

    取哪个 ABC 由 `effective_abc_class` 定（单据优先、物料兜底）。两者都空 ⇒ 降级而不是
    给 0.00 —— 给 0 会让「ABC 未派生」与「C 类」在库里长得一样，而前者要写进
    `factor_degraded`（`19` 的四类空输入之一）。
    """
    grade_source = effective_abc_class(order_abc_class, material_abc_class)
    if grade_source is None:
        return FactorOutcome.degraded(
            "ABC 分类未导入（单据与物料主数据均无 ABC 等级）——该因子无从取数"
        )
    abc_class = _as_member(grade_source)
    grade = ABC_GRADE[abc_class]
    return FactorOutcome.scored(grade / ABC_GRADE_MAX, f"{abc_class.value} 类（{grade}/3）")


def cap_factor(*, available: int, cap_total: int) -> FactorOutcome:
    """`cap`：可用 ÷ `cap_total`（`17` §3.4 的三列口径）。

    分母取 `cap_total`（**净额**，已是「总格数 − 已占格数」）而不是总格数：`17` §10.1
    的算例里 `58 / 80 → 0.72` 就是这个分母。`available` 由 `reserved.available_cap()`
    给（`design.md` D7）——**不在这里重算第二个「可用」口径**。

    夹取到 `[0,1]`：对账不平的数据（`available > cap_total`，如 cap 漂移未对账）会让
    裸比值越界，而 `FactorTerm` 的 `[0,1]` 是契约。夹取只影响越界那一档的**相对**次序，
    真正要报的是 `cap` 对账告警（阶段四 F9）。
    """
    value = 0.0 if cap_total <= 0 else min(1.0, max(0.0, available / cap_total))
    return FactorOutcome.scored(value, f"可用 {available} / {cap_total} 板")


def require_order_cells(order_cells: int) -> None:
    """本单占用格数必须 > 0，否则 `ValueError`。

    两个消费方都要它：`existing_factor` 拿它当分母；`scoring.feasible_aisles`（3.2）拿它
    与可用容量比。两处的失效形态都是**反向**的（分母为 0 / `可用 >= 0` 恒真 ⇒ 一张 0 格的
    单拿到一份「近站台最优」的推荐），所以判据与文案只有这一份 —— 分开写会各自漂移，
    而它们的依据是同一条：`16` §171 把「数量 > 0」列为导入期必须阻断的口径异常。

    本函数是**公共**的（`factors.py` 与 `scoring.py` 都用），故不带下划线前缀。
    """
    if order_cells <= 0:
        raise ValueError(
            f"本单占用格数必须 > 0（收到 {order_cells}）——`16` §171 把「数量 > 0」列为"
            "导入期必须阻断的口径异常，0 格的单不该走到评分这一步"
        )


def existing_factor(
    *, profile: InventoryProfile, aisle: str, order_cells: int
) -> FactorOutcome:
    """`existing`：`min(1, 该巷既有板数 ÷ 本单占用格数)`。

    分母是**本单**占用量，不是巷道容量：`17` §10.1 的算例里两条巷道的容量都是 80，而
    既有板数 6 / 2 / 0 给出 0.60 / 0.20 / 0.00，同分母 10（该算例本单 10 板）。这条
    正是 `SC-005`「既有同物料落位抬升同巷道得分」在**因子层**的落点。
    """
    require_order_cells(order_cells)
    if not profile.snapshot_present:
        return _inventory_degrade(profile)
    plates = profile.plates_by_aisle.get(aisle, 0)
    if plates == 0:
        return FactorOutcome.scored(0.0, "无既有库存")
    return FactorOutcome.scored(min(1.0, plates / order_cells), f"既有 {plates} 板集中于此")


def station_factor(*, distance_weight: float | None) -> FactorOutcome:
    """`station`：站台距离权重，夹取到 `[0,1]`。

    `distance_weight` 为 `None` 即**该巷道没有 `AisleStation` 行** —— 取数侧
    （`load_station_weights`）用「不在字典里」表达它，故这里不需要另设一个「有没有导出」
    的入参：**没有这一行就是没有数据**，两种成因（整表未导出 / 该巷未挂站台）都由调用方
    的取值路径自然落到同一个 `None` 上。

    夹取的依据：`17` §2.2 的列注释只给了 `0.9（近）/ 0.3（远）` 两个示例值，**未声明值域
    上界**（模型也刻意不加 `[0,1]` 的 CHECK —— 业务方可能给的是米数）。而 spec 要求每个
    因子归一化到 `[0,1]`，故在**读取侧**夹取，把未定的口径隔离在这一个函数里（`design.md`
    D14 的 Open Questions 第 3 条；口径到齐后只改这里）。

    **方案级注意**：本函数是**一条巷道**的取值。一条巷缺行时，该因子必须在**全部**候选
    上一并降级 —— 理由见模块 docstring 第 4 条。这条规则由 `scoring.py`（3.3）按候选集
    聚合，不在这里：函数只有一条巷道的视野。
    """
    if distance_weight is None:
        return FactorOutcome.degraded(
            "无该巷道的站台主数据（AisleStation 未导出，或该巷道未挂出站台）——该因子无从取数"
        )
    clamped = min(1.0, max(0.0, float(distance_weight)))
    if clamped != distance_weight:
        # 取值与原始口径都给出来：操作员要能看出「1.0 是夹出来的」而不是「本来就是 1.0」，
        # 否则真实导出（例如米数）会看起来像一堆满分巷道。
        return FactorOutcome.scored(
            clamped, f"站台距离权重 {distance_weight}（超出 [0,1]，夹取到 {clamped:.2f}）"
        )
    return FactorOutcome.scored(clamped, f"站台距离权重 {distance_weight}")


def batch_factor(
    *, profile: InventoryProfile, aisle: str, order_batch_no: str | None
) -> FactorOutcome:
    """`batch`：本单批号 ∈ 该巷既有批号集 ⇒ `1.00`，否则 `0.00`（二值）。

    **读库存快照的既有批号集，不读 PO 文件**（`SC-003`）：`16` A.4 的 PO 模版没有批号列。
    本单批号则来自系统在**入库单建立时按生产批规则生成**的字段（`14` §3.1，同一生产批
    共用同一批号），故分配时刻已知。`17` §10.1 的算例里 `02` 有 2 板却判「无同批」，
    说明比的是**具体批号**而不是「有没有这个料」。

    **首次到货**（该批号在库里从未出现过）时全巷道 0.00：一个对所有候选相同的值不改
    相对次序，只把各巷总分等比缩小（`design.md` D16）。

    批号为空 ⇒ 降级，理由见模块 docstring 第 3 条。
    """
    if order_batch_no is None:
        return FactorOutcome.degraded(
            "本单无批号（入库单未按生产批生成批号）——无从比较，非「无同批」"
        )
    if not profile.snapshot_present:
        return _inventory_degrade(profile)
    if order_batch_no in profile.batches_by_aisle.get(aisle, frozenset()):
        return FactorOutcome.scored(1.00, f"同批 {order_batch_no} 已在此巷道")
    return FactorOutcome.scored(0.00, "无同批")


def continuity_factor(*, profile: InventoryProfile, aisle: str) -> FactorOutcome:
    """`continuity`：该巷在该物料既有巷道集内 ⇒ `1 ÷ 跨巷道数`，否则 `0.00`。

    `17` §10.1 的算例：同物料现跨 2 个巷道 → `0.50`、现仅在此巷道 → `1.00`、
    不在该巷道 → `0.00`。倒数而不是线性：把「再多跨一条巷道」的代价固定成一档，
    于是「并回既有巷道」的收益随当前跨巷道数递减 —— 跨得越多，回一条越值钱。
    """
    if not profile.snapshot_present:
        return _inventory_degrade(profile)
    count = profile.cross_aisle_count
    if count == 0:
        # 该料号在库里还没有任何库存：三个读库存因子里只有它会遇到这形态，取值 0.00
        # 与 `existing` 的「无既有库存」同源（空档案，不是降级）。
        return FactorOutcome.scored(0.0, "无既有库存")
    if aisle not in profile.aisles:
        return FactorOutcome.scored(0.0, "同物料不在该巷道")
    if count == 1:
        return FactorOutcome.scored(1.0, "同物料现仅在此巷道")
    return FactorOutcome.scored(1 / count, f"同物料现跨 {count} 个巷道")
