"""6 类 JSON 结构（对外契约）。

事实来源：17-数据模型设计 §十
  1. 推荐理由（入库）  job_id / aisles / factors{six} / scores / degraded / degrade_reason
  2. 顺路取顺序（出库）do_no / pick_sequence / weighted_concentration / threshold_n / exceeded
  3. 收拢方案（移库）  batch_no / material_code / from_aisles / target_aisle / plates /
                       expected_cross_aisle{before,after} / batch_unchanged
  4. 导入校验回执      session_id / data_time / files[{file_type,rows,fields_hit,anomalies,status}]
  5. cap 快照          snapshot_version / aisles[{aisle,total,reserved,usable,near_station}]
  6. KPI 卡片          period / weighted_concentration / same_material_cross_aisle /
                       same_batch_cross_aisle / adoption_rate / placement_accuracy

硬约束：降级时 degraded=true 且 degrade_reason 必填（降级不静默）。

## 本文件当前落地的部分（阶段三）

**结构 1（§10.1）、结构 2（§10.2）已落地**；**结构 3~6 仍是骨架** ——
它们分别属移库（阶段四）、导入回执（阶段四）、cap 快照（阶段四）、KPI 卡片（阶段六）。不预先补齐的理由：
那几类的字段要等各自的实现去校准，先写一份没人用的契约，只会在实现时变成
「改也不是、不改也不是」的第二事实来源。

## 三处口径由 design.md 定，不在这里重述

- `aisles` 取**单条**（得分最高者；并列同分则全列，扣减落在 `aisle_no` 最小者）—— `D16`。
- 六因子的归一化口径与各自的反推依据 —— `D16` 的表（`17` §10.1 只给了示例值）。
- `factor_degraded` 与方案级 `degraded` **不共用字段**（`D8`），故两者各有自己的必填校验。

## 为什么不把「六因子」写成这里的字面量

`factors` 的键集必须恒等于 `configuration.WEIGHT_FACTORS`。**直接引用那个元组**而不是
在这里再写一遍六个名字：两处各写一份的话，加因子时总有一处漏改，而漏改的表现是
「评分算了六项、契约只收五项」这种不报错只失真的形态。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import AbcClass
from app.models.configuration import WEIGHT_FACTORS

#: 六因子的**唯一**名字来源（`14` §3.1 的固定因子集，不得增删）。
#: 是**别名**而不是副本 —— 见模块 docstring 末节。
FACTOR_NAMES: tuple[str, ...] = WEIGHT_FACTORS

#: 单次批量分配的单据数上限（`design.md` D10 / spec「规模上限与评分性能」）。
#: **不写成 `Field(max_length=...)`**（8.5 落地时确认过这条）：那样超限会得到一条
#: FastAPI 默认形状的 422（`{"detail": [...]}`，没有 `error` / `message`），而本端点把
#: 领域错误统一在 `errors.error_body` 上、且文档要求的是「拒绝并**提示拆分**」——
#: 提示语得由端点给。故上限在这里只作常量，判断落在路由层 `_require_batch_size`。
MAX_JOB_ORDERS_PER_BATCH = 50

#: `snapshot_version` 的展示形状（`17` §10.7 的示例串）。
#: `Snapshot` 存的是 `version_no` 整数，这个串是 `snapshot_time` 的渲染（`design.md` D10）。
#: 在报文层钉住形状，是为了让「误把 `version_no` 接进来」当场失败 —— 那正是 D10 的
#: Risks 条目点名要防的接错。
_SNAPSHOT_VERSION_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"


class FactorTerm(BaseModel):
    """一个「巷道 × 因子」的取值与取值说明（`17` §10.1 的 `breakdown` 叶节点）。"""

    #: 归一化到 `[0,1]` 的取值。上界由因子函数保证（`cap` 因子对账不平的数据做夹取），
    #: 在这里再钉一道：越界说明因子函数漏了归一化，属实现缺陷、不该流到前端。
    value: float = Field(ge=0.0, le=1.0)
    #: 取值说明，**用原始口径**（「可用 58 / 80 板」而不是「0.72」）—— `22` §三 的因子行
    #: 要让操作员看懂「为什么是这个分」，复述归一化后的数等于什么都没说。
    note: str = Field(min_length=1)


class PriorityTerms(BaseModel):
    """`priority.score` 的三项加法分量（`14` §3.2）。各自已归一化到 `[0,1]`。"""

    #: 未来 N 天交货单出库量（归一化）。
    outbound_qty: float = Field(ge=0.0, le=1.0)
    #: ABC 等级分。`17` §10.1 的示例给 `A → 1.00`，与 `abc` 因子同一套（等级分 ÷ 3）。
    abc: float = Field(ge=0.0, le=1.0)
    #: 既有集中度增益 —— 「并回既有巷道可降跨巷道数的幅度」（`14` §3.2）。
    existing_gain: float = Field(ge=0.0, le=1.0)


class PriorityPayload(BaseModel):
    """本单在当日队列中的优先级（`17` §10.1 的 `priority`，口径见 `14` §3.2）。

    三项加法：未来 N 天交货单出库量 + ABC 等级分 + 既有集中度增益。
    """

    score: float
    terms: PriorityTerms
    #: **排序降级**（第三处降级标记，与方案级/因子级都不是一回事，`design.md` D8）。
    #: 缺未来 N 天出库量时退化为仅按 ABC 排序 —— 不中断分配，但必须说得出来。
    degraded: bool = False
    degrade_reason: str | None = None

    @model_validator(mode="after")
    def _degrade_requires_reason(self) -> PriorityPayload:
        if self.degraded and not self.degrade_reason:
            raise ValueError("排序降级必须写明 degrade_reason（降级不静默）")
        return self


class PredictedCrossAisle(BaseModel):
    """预测跨巷道（`17` §10.7）。

    口径：`| 既有快照中该物料占用的巷道 ∪ 本次分配给该物料的巷道 |` —— 即把
    `18` §1.3「同物料跨巷道数 ≤5」在**分配时**预演一次（时点不同，口径相同）。
    """

    material: int = Field(ge=0)
    threshold: int = Field(ge=1)
    exceeded: bool

    @model_validator(mode="after")
    def _exceeded_follows_from_material(self) -> PredictedCrossAisle:
        if self.exceeded != (self.material > self.threshold):
            raise ValueError("exceeded 必须等于 material > threshold（17 §10.7 的定义式）")
        return self


class ReasonPayload(BaseModel):
    """推荐理由（`17` §10.1）—— `RecommendationPlan.payload_json` 的形状。

    写入方只有一个（评分引擎），列级 `degraded` / `degrade_reason` 是它的**投影**
    （`17` §4.2 的模型注释）。两者不一致属写入侧 bug，故 `D9` 把「列与 JSON 一致」
    列为不变量之一，由 `test_reasons.py` 钉住。
    """

    model_config = ConfigDict(extra="forbid")

    #: 作业单标识。与 §10.7 的 `job_order_id` 同源同值（该字段的选型见 D16）。
    job_id: str = Field(min_length=1)

    #: 本次选中的推荐巷道集 —— **单条**；并列同分时全列并按 `aisle_no` 升序（D16）。
    aisles: list[str] = Field(min_length=1)

    #: 六项权重。**恒为六项**，含降级因子在内（§10.1 的算例：`station` 降级后
    #: `factors` 里仍有它的 0.20，分母 0.80 就是把它刨掉的结果）。
    factors: dict[str, float]

    #: 每巷道总分，键集 = 候选巷道集（= `breakdown` 的键集）。
    scores: dict[str, float]

    #: 每巷道 × 每参与因子的取值与取值说明。
    breakdown: dict[str, dict[str, FactorTerm]]

    priority: PriorityPayload

    #: **因子级降级**：键为因子名、值为原因。出现于此的因子不参与评分，也不在 `breakdown` 中。
    factor_degraded: dict[str, str] = Field(default_factory=dict)

    #: **方案级降级**（容量不足走了降级链）。与 `factor_degraded` 不共用字段（D8）。
    degraded: bool = False
    degrade_reason: str | None = None

    #: 预测跨巷道（`17` §10.7 的响应侧字段，`design.md` D1 要求它随方案一起留存）。
    #: 分配器在队列全部处理完后回溯计算，故不是「每个候选巷道」的属性，而是**本单**的属性。
    predicted_cross_aisle: PredictedCrossAisle

    @model_validator(mode="after")
    def _contract_invariants(self) -> ReasonPayload:
        factor_keys = set(self.factors)
        if factor_keys != set(FACTOR_NAMES):
            missing = sorted(set(FACTOR_NAMES) - factor_keys)
            extra = sorted(factor_keys - set(FACTOR_NAMES))
            raise ValueError(f"factors 必须恰为六因子（缺 {missing}、多 {extra}）")

        unknown = sorted(set(self.factor_degraded) - factor_keys)
        if unknown:
            raise ValueError(f"factor_degraded 含未知因子 {unknown}")

        if set(self.breakdown) != set(self.scores):
            raise ValueError("breakdown 的键集必须与 scores 一致（都等于候选巷道集）")

        # 参与评分 = 六因子 − 降级因子。每巷道的分解必须**恰好**是这一组：
        # 少一个是「有因子没给取值」，多一个是「降级因子混进来了」，两者都要拦。
        scored = factor_keys - set(self.factor_degraded)
        for aisle, terms in self.breakdown.items():
            if set(terms) != scored:
                raise ValueError(
                    f"巷道 {aisle} 的分解键集必须恰为参与评分的因子 "
                    f"（缺 {sorted(scored - set(terms))}、多 {sorted(set(terms) - scored)}）"
                )

        if not set(self.aisles) <= set(self.scores):
            raise ValueError("aisles 必须是候选巷道集的子集")
        if len(set(self.aisles)) != len(self.aisles):
            raise ValueError("aisles 不得重复")

        if self.degraded and not self.degrade_reason:
            raise ValueError("方案级降级必须写明 degrade_reason（降级不静默）")
        return self


class BatchAllocateRequest(BaseModel):
    """`POST /api/allocate/batch` 的请求体（`17` §10.7）。"""

    #: 未知字段直接 422 —— 与 `LoginRequest` 同一处置：让「把 `job_order_ids` 拼成
    #: `job_order_id`」在联调时就暴露，而不是被静默忽略成「这单没参与分配」。
    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)

    #: 调用方声明的快照版本（§10.7 的示例串形状）。语义见 D16：它是**声明**不是筛选器，
    #: 与本次实际使用的快照不一致时端点拒绝，避免「以为钉住了快照、其实没有」。
    snapshot_version: str | None = Field(default=None, pattern=_SNAPSHOT_VERSION_PATTERN)

    #: 参与本次容量竞争的作业单集合。**必填**：不给默认值，就没有「空即全量」的隐含默认，
    #: 空数组是明确的空集（§10.7 的硬要求，与「同样输入必得同样输出」直接相关）。
    #:
    #: **刻意不带 `max_length`**（上限只作常量，判断在路由层，理由见上方常量处）。
    job_order_ids: list[str]


class PlanItem(BaseModel):
    """响应里的一条方案（`17` §10.7 的 `plans[]`）。

    **不含理由体** —— 6 因子分值、每因子取值说明与两种降级标记都在 `payload_json` 里，
    按 `plan_id` 另取。同一份理由有两个副本，就有两个可能分叉的副本（`design.md` D10）。
    """

    job_order_id: str = Field(min_length=1)
    order_no: str = Field(min_length=1)
    material_code: str = Field(min_length=1)
    material_name: str | None = None
    #: 可空：ABC 由成品清单聚合派生，派生完成前单子已经可以入队（`JobOrder.abc_class`）。
    abc_class: AbcClass | None = None
    qty: int
    aisles: list[str] = Field(min_length=1)
    predicted_cross_aisle: PredictedCrossAisle
    #: 该队列项的优先级，与理由里的 `priority.score` 同值（§10.7 的字段表）。
    priority: float
    #: 指向 `RecommendationPlan` 的行 id。
    plan_id: int


class DegradedAlert(BaseModel):
    """A 类爆款被迫降级的告警（`14` §3.5）—— 「降级不静默」的对外出口。

    `17` §10.7 未逐字段定义本结构，三处口径按此实现在 `design.md` D16：
    `job_order_id` 同 §10.7 的选型；`aisle` = **降级后实际落到的巷道**（与
    `plans[].aisles` 对齐，故可读出「它为什么去了那条巷道」）；`message` 含近站台缺口量。
    """

    job_order_id: str = Field(min_length=1)
    aisle: str = Field(min_length=1)
    message: str = Field(min_length=1)


class BatchAllocateResponse(BaseModel):
    """`POST /api/allocate/batch` 的响应体（`17` §10.7）。

    `plans` 按 `priority` 降序 —— 即「谁先挑」的顺序，也是 UI 队列的默认序。
    """

    bulk_batch_no: str = Field(min_length=1)
    #: `Snapshot.snapshot_time` 的 `"%Y-%m-%dT%H:%M"` 渲染（不是 `version_no`，D10）。
    snapshot_version: str = Field(pattern=_SNAPSHOT_VERSION_PATTERN)
    plans: list[PlanItem] = Field(default_factory=list)
    degraded_alerts: list[DegradedAlert] = Field(default_factory=list)


# ------------------------------------------------------------------ 结构 2：顺路取顺序（17 §10.2）

class PickPathItem(BaseModel):
    """顺路取序列里的一条巷道（17 §10.2 的 `pick_sequence` 元素形）。

    也是 `ConfirmItem.pick_path` 的元素（`app/schemas/job.py` 引用它）—— 巷道号按 2 位
    文本（库位号 `[:2]`），前导 0 不得丢（CLAUDE.md §七）。
    """

    aisle: str = Field(min_length=2, max_length=2)
    #: 该巷拣货量（= 现状库存量，`derive_pick_sequence` 不跨巷分配）。
    qty: int = Field(ge=0)
    #: 该巷批号集，升序（确定性）。
    batches: list[str] = Field(default_factory=list)


class PickSequence(BaseModel):
    """一条 DO 的顺路取顺序（17 §10.2）：`do_no` / `pick_sequence` /
    `weighted_concentration` / `threshold_n` / `exceeded`。

    加 `snapshot_version` 簿记字段（design.md D3：本方案基于的库存视图版本，随方案
    版本化）—— 只加不删，17 §10.2 的消费方忽略未知键。它同时是
    `RecommendationPlan.payload_json` 的形状与响应 `plans[]` 元素的基形。
    """

    model_config = ConfigDict(extra="forbid")

    do_no: str = Field(min_length=1)
    pick_sequence: list[PickPathItem] = Field(default_factory=list)
    #: 拣货量加权集中度 = 80% 降序累加所覆盖的巷道数（`concentration_aisle_count`）。
    weighted_concentration: int = Field(ge=0)
    threshold_n: int = Field(ge=1)
    #: `weighted_concentration > threshold_n`，仅高亮不阻断（15-03 §6.2）。
    exceeded: bool
    snapshot_version: str = Field(pattern=_SNAPSHOT_VERSION_PATTERN)


class PickPlanItem(PickSequence):
    """响应 `plans[]` 里的一条方案：`PickSequence` + 对应回请求的作业单与方案行。

    `job_order_id` / `plan_id` 是响应侧标识（对齐 `PlanItem`），**不进 payload_json**
    —— 方案归属是 `RecommendationPlan` 的列，不是 17 §10.2 的内容。
    """

    job_order_id: str = Field(min_length=1)
    plan_id: int


class BatchPickSequenceRequest(BaseModel):
    """`POST /api/job/batch/pick-sequence` 的请求体（D2）。

    与 `BatchAllocateRequest` 同一形态：`job_order_ids` 必填非空、`snapshot_version`
    是调用方声明（不是筛选器）。上限 `MAX_JOB_ORDERS_PER_BATCH` 只作常量、判在路由层。
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    snapshot_version: str | None = Field(default=None, pattern=_SNAPSHOT_VERSION_PATTERN)
    job_order_ids: list[str]


class BatchPickSequenceResponse(BaseModel):
    """`POST /api/job/batch/pick-sequence` 的响应体（D2：分列 `plans[]` + `not_in_stock[]`）。

    `not_in_stock[]` = 货未入库、无法生成顺路取的单（提示「该品项尚未入库，暂无法生成
    顺路取」，不阻断，单停留 `PENDING`）。
    """

    bulk_batch_no: str = Field(min_length=1)
    snapshot_version: str = Field(pattern=_SNAPSHOT_VERSION_PATTERN)
    plans: list[PickPlanItem] = Field(default_factory=list)
    not_in_stock: list[str] = Field(default_factory=list)
