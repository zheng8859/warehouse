"""作业链 5 实体：作业单 → 推荐方案 / 台账 / 后验记录 → 偏离批次。

事实来源：17-数据模型设计 §四（作业与交易实体）、§10.1~10.3（三类方案 JSON）
          15-入库出库移库与后验流程设计 §3（状态机）、§6.2/§6.3（逐单处置与写台账）、
            §7（后验与偏离）、§11.7（重复执行与幂等）、附录 A（三类台账字段矩阵）
          openspec/changes/data-model-permission/design.md D1 / D2 / D3 / D4 / D5 / D8
          spec `data-model`「JobOrder 状态机」「乐观锁并发守卫」「枚举登记范围与取值」

实体：JobOrder(作业单，三类共用) / RecommendationPlan(推荐方案) / Ledger(台账)
      Verification(后验记录) / Deviation(偏离批次)

状态迁移的判定不在这里 —— 它是纯函数，在 `app/core/state_machine.py`（理由见那个模块）。

## 关键口径

- 三类作业共用一张作业单表，以 `job_type` 区分（17 §4.1 / 26 Step 4）
- 台账三类合一，以 `ledger_type` 区分，**取值域复用 `job_type`**（17 §九⑤）
- 台账由引擎与作业流自动写，不向任何角色开放手动入口（15 §4.3 表注 / `13` 矩阵）
- 偏离批次是**移库作业的任务来源**，移库后偏离状态推进（15 §7.2）—— 治理回路的闭环

## 本模块的十一处取舍（文档没直说或两处口径不齐，评审要看的就是这些）

1. **17 §4.1 的「推荐侧 / 后验侧 / 降级标记」不在 `job_orders` 上**。§4.1 是按聚合根
   罗列的属性分组，§4.2 / §4.4 才把它们各自定义成实体（`RecommendationPlan` /
   `Verification`）。同一事实在两处各存一份，就有了两个可写的副本 ——
   而 15 §7.3 那条追溯链「推荐理由 → 确认记录 → 实际落位 → 后验结果」指的是哪一份，
   将没有答案。作业单只留**执行侧**（实际库位 / 实际数量 / 执行时间），因为那是
   作业单自己的状态推进，不是任何别的实体能替它记的。
2. **`confirmed_by_id` 一列同时承担 §4.1 的「操作人」与 §4.5 的「确认人」**。
   15 §6.3 的写入时机是「二次确认卡通过后才写台账」，`CONFIRMED → EXECUTED` 是同一个
   动作的两步 —— 确认者即执行者。分两列存同一个人的两个 id，只会给「两列不一致」留位置。
3. **`bulk_batch_no` 不叫 `batch_no`**。17 §4.1 的「批量批次 ID」与 `Batch` / `InventoryItem`
   / `Ledger` 里的「批号」是两个东西：前者是「一次批量操作生成的批次，便于整体追溯」，
   后者是生产批号。同一个名字给两件事用，`job_orders.batch_no` 到底指哪个要靠上下文猜。
4. **唯一键 =（`warehouse_id`, `job_type`, `order_no`, `line_no`）**。16 附录A 的行唯一键是
   「单据号码 + 行号」，那是**同一份文件内**的判重口径；三类单据的号码来自三个编制方
   （PO 来自生产、DO 来自发货、移库任务号由本系统生成），跨类撞号是误判而非真重复。
5. **`recommendation_plans.job_order_id` 不唯一**。15 §3.1 允许 `PENDING → PENDING`
   （分配失败重试）与 `REJECTED → PENDING`（驳回后重新入队）—— 同一张单会有多份方案，
   历史保留可追溯。「当前方案」不加指针列，取该单 `id` 最大的一行 —— 与 D1 对配置版本
   「生效时间 ≤ now 中 `version_no` 最大者」是同一手法：指针与版本表双写，
   指针写失败即产生静默的不一致。
6. **`degraded` / `degrade_reason` 落成真列，而不只活在 `payload_json` 里**。17 §10.1 的
   JSON 形状里确实有这两个键（照存，前端与阶段三可以直接渲染原文），但
   「**降级不静默**」这条要求得落成 DB CHECK 才算数，而 JSON 字段在 SQLite 上无法参与
   CHECK。故两列是 JSON 同名字段的**投影**：写入方只有一个（阶段三的评分引擎），
   两者不一致属写入侧 bug —— 测试里两处都钉住。
   CHECK 只钉一个方向（降级 ⇒ 必有原因），理由见 `_DEGRADE_CHECK` 的注释。
7. **台账的唯一键落在 `job_order_id`**，不是（`ledger_type`, `order_no`）。一张 PO 有多行
   （16 附录A 的行唯一键 = 单据号码 + 行号），而台账按 15 附录A **不记行号** ——
   用（类型, 单号）做键会让多行单据的第二行写不进去，把「防重复」变成「防多行」，
   而红线要防的是「同一作业单重复写台账」（15 §11.7）。
8. **台账不存后验结论**。15 附录A 的矩阵里给了「后验结果」一行，但 17 §4.3 的台账字段
   矩阵**没有这一行**，而 17 是实体字段的单一事实来源（附录A 自己也是这么写的）。
   还有一条时序理由：15 §6.3 的顺序是「台账写入成功后自动触发后验」—— 写台账那一刻
   后验结果还不存在，台账上若有这两列就必然是后写的，而台账写入即固化（表上没有
   `updated_at`，与 `Snapshot` 同一手法：缺这一列本身就是「这张表不该被改写」的可验事实）。
9. **`verifications.metric_kind` 不建枚举**。D2 列出的四类局部值域不含它，17 §4.4 也只写
   「后验指标」；凭空加第五个值域会让「11 个共享枚举 + 4 个局部值域」这个被三处引用的
   口径当场失真。代价是错拼拦不住（只会多出一行指标），已登记在 tasks.md 9.4c。
   取值即 15 §7.1 的口径名，一行一条指标 —— 唯一键 =（作业单, 指标）。
10. **`deviations.relocate_job_order_id`**。26 附录A 的依赖链写的是
    `Verification → Deviation → JobOrder`，15 §7.2 说偏离清单是移库的任务来源 ——
    两处说的都是这条回环。没有这一列，局部值域里的「已发起移库」就只是自述：
    它与「有人改过这格」在库里长得一样。
11. **两处指向 `accounts` 的引用是分两步建键的**：`job_orders.confirmed_by_id` 与
    `ledgers.operator_id`（26 附录A 的 `Account → JobOrder`）。账号表属身份链，而
    SQLAlchemy 的 `ForeignKey` 目标表必须已在 `Base.metadata` 中 —— 否则 `create_all`
    与迁移在编译 DDL 时抛 `NoReferencedTableError`，本组的建表与测试全部跑不起来。
    故 §4 先落整数列，**§5 落地 `accounts` 后由迁移 `0559bebb5207` 用
    `batch_alter_table` 补上两条外键**（SQLite 改约束只能 batch 重建）—— 与 §3 的
    `cap_alerts.ledger_txn_id` 同一处理。tasks.md 9.4d① 已据此销账。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AbcClass, Disposition, JobStatus, JobType, LedgerType, VerifyResult
from app.models.base import BaseEntity, enum_column, utcnow


class PlanKind(str, Enum):
    """方案类型（17 §4.2）。

    **实体局部值域**：不进 `app/core/enums.py`（17 §九 只登记跨模块共享的 11 个，
    design.md D2）。取值即文档原文 —— 与 `AlertKind` / `ItemStatus` 同一处理
    （成员名 ASCII、取值照原样），因为这三类值会直接出现在批量方案表的列头与
    推荐理由里，翻译成代号反而多一层映射。

    与 `JobType` 一一对应（入库→分配、出库→顺路取、移库→收拢）。既然可互推，
    它存在的理由就不是「表达新信息」，而是**让错配可被拒绝**。
    """

    ASSIGN = "分配"
    PICK = "顺路取"
    CONSOLIDATE = "收拢"


class DeviationStatus(str, Enum):
    """偏离批次状态（17 §4.4）。**实体局部值域**（D2）。

    `RELOCATE_STARTED` 必须指向一张移库作业单（见 `_DEVIATION_RELOCATE_CHECK`）。
    """

    OPEN = "未处理"
    RELOCATE_STARTED = "已发起移库"
    IMPROVED = "已改善"


class DeviationCauseKind(str, Enum):
    """偏离成因分类（17 §4.4）。**实体局部值域**（D2）。

    它决定处置路径（15 §7.2）：新入库收拢不达标 → 查引擎与配置；
    历史库存拖累 → 走移库补救。取值放开口，等于把处置路径交给自由文本。
    """

    NEW_INBOUND_SHORTFALL = "新入库收拢不达标"
    LEGACY_INVENTORY_DRAG = "历史库存拖累"


#: 「降级不静默」（17 §10.1）。
#: **只钉一个方向**：降级 ⇒ 必有原因。反方向（记了原因却没标降级）不拦 ——
#: 14 §4.4 的降级链按因子逐条触发，「部分因子降级、整体未降级」是可能出现的取值，
#: DB 拦死会需要一次迁移才能放开，而它并不是红线要防的东西。
_DEGRADE_CHECK = "(degraded = 0) OR (degrade_reason IS NOT NULL)"

#: 15 附录A 的三类台账字段矩阵：入库无源库位、出库无目标库位、移库两者都有。
#: 把矩阵写成 CHECK 而不是留给服务层自觉 —— 「入库台账里冒出一个源库位」在按巷道
#: 汇总 cap 增量时（16 §6.3）会被当成一次出库，静默地多减一格。
_LEDGER_LOCATION_CHECK = (
    f"(ledger_type = '{LedgerType.INBOUND.value}'"
    " AND source_location_code IS NULL AND target_location_code IS NOT NULL)"
    f" OR (ledger_type = '{LedgerType.OUTBOUND.value}'"
    " AND source_location_code IS NOT NULL AND target_location_code IS NULL)"
    f" OR (ledger_type = '{LedgerType.RELOCATE.value}'"
    " AND source_location_code IS NOT NULL AND target_location_code IS NOT NULL)"
)

#: 库位号一律 6 位文本（spec `data-model`「库位号按 6 位文本处理」）。
#: 可空列用 `IS NULL OR` 起头 —— SQLite 里 `NULL AND ...` 是 NULL，
#: 而 CHECK 把 NULL 视为通过，写成 `length(x) = 6` 会让空值意外放行**且**读不出意图。
_LOCATION_LEN = "length({col}) = 6"


class JobOrder(BaseEntity):
    """作业单，三类共用（17 §4.1 / 15 §3.3）。

    状态机见 `app/core/state_machine.py`；`lock_version` 与 `ImportSession` 同为 D1 的
    乐观锁名单（15 §10.6「防止多端同时确认同一作业单」）。
    """

    __tablename__ = "job_orders"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id",
            "job_type",
            "order_no",
            "line_no",
            name="uq_job_orders_warehouse_type_order_line",
        ),
        sa.CheckConstraint(
            f"actual_location_code IS NULL OR {_LOCATION_LEN.format(col='actual_location_code')}",
            name="actual_location_code_len6",
        ),
    )

    #: 业务单号：入库 = PO 号、出库 = DO 号、移库 = 移库任务号（17 §4.1）。
    #: `3573743144K55G` 是 16 附录A 字段映射表里的真实样本，故留 64 位余量。
    order_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 行号。16 附录A 明确它是**文本**（示例 `10`）—— 存成整数会丢掉 `0010` 这类前导 0，
    #: 而它在联合唯一键里，弄丢了就判不出重复导入。
    line_no: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    job_type: Mapped[JobType] = enum_column(JobType, name="job_type", nullable=False)

    #: 所属批量批次 ID（17 §4.1「一次批量生成一个批次，便于整体追溯」）。
    #: 可空：单子由**导入**入队时还没有批量。名字的由来见模块 docstring 第 3 条。
    bulk_batch_no: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    material_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    material_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)

    #: 数量（17 §4.1「箱数/板数」）。单位随源单据（PO 给计划入库量、DO 给拣货量），
    #: 统一按源值存 —— 折算成格数是推荐侧的事（16 A.4 的板-格换算）。
    #: 「箱 / 板」的口径未在文档中定死，登记在 tasks.md 9.4c，不在此处编一个单位列。
    qty: Mapped[int] = mapped_column(nullable=False)

    #: ABC 分类。可空：它由成品清单聚合派生（16 A.4），派生完成前单子已经可以入队。
    abc_class: Mapped[AbcClass | None] = enum_column(AbcClass, name="abc_class", nullable=True)

    #: 生产批号。可空：16 附录A 的 PO / DO 模版**都没有批号列**（PO 只有生产日期）——
    #: 入库单的批号由**系统在入库单建立时按生产批规则生成**（同一生产批共用同一批号，
    #: `17` §2.4；分配时刻已可读，规则本身属阶段四）；出库单的批号由顺路取从库存明细里
    #: 选出（17 §10.2）。
    batch_no: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    status: Mapped[JobStatus] = enum_column(
        JobStatus, name="job_status", nullable=False, default=JobStatus.PENDING
    )

    #: 乐观锁（D1）。与 `ImportSession` 同为名单内的两个实体。
    lock_version: Mapped[int] = mapped_column(nullable=False, default=0)

    #: 逐单处置（17 §4.5 / 15 §6.2）。确认前为空。
    disposition: Mapped[Disposition | None] = enum_column(
        Disposition, name="disposition", nullable=True
    )

    #: 确认（= 执行）人。可空：未确认的单据没有确认人。外键由 §5 的迁移补上
    #: （见模块 docstring 第 11 条）—— 「未确认不产生台账」这条红线要查得下去，
    #: 单据上写着「已确认」就必须指得到人。
    confirmed_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("accounts.id"), nullable=True
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: 确认卡摘要 / 微调内容 / 驳回原因（17 §4.5）。用 Text 存：
    #: 摘要是给人看的复核凭据，长度由渲染决定，不设人为上界。
    confirm_card_digest: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    tune_detail: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: 执行侧（17 §4.1）：实际落位的库位与数量。库位按 6 位文本（见 `_LOCATION_LEN`）。
    actual_location_code: Mapped[str | None] = mapped_column(sa.String(6), nullable=True)
    actual_qty: Mapped[int | None] = mapped_column(nullable=True)

    executed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: 作业单会被反复改状态 → 需要 `updated_at`（`Snapshot` / `Ledger` 这类追加式表则没有）。
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)


class RecommendationPlan(BaseEntity):
    """推荐方案（17 §4.2）。三类方案共用 `payload_json`，以 `plan_kind` 区分。

    `payload_json` 的形状即 17 §10.1（推荐理由）/ §10.2（顺路取顺序）/
    §10.3（收拢方案）—— D4 只指定落列，结构仍以 17 §10 为准。
    """

    __tablename__ = "recommendation_plans"
    __table_args__ = (
        sa.CheckConstraint(_DEGRADE_CHECK, name="degrade_reason_required"),
        # 取「当前方案」的查询路径 =（该单, id 最大），见模块 docstring 第 5 条。
        sa.Index("ix_recommendation_plans_job_order_id", "job_order_id"),
    )

    #: 所属作业单。**不唯一**：同一张单可以有多份方案（见模块 docstring 第 5 条）。
    job_order_id: Mapped[int] = mapped_column(sa.ForeignKey("job_orders.id"), nullable=False)

    plan_kind: Mapped[PlanKind] = enum_column(PlanKind, name="plan_kind", nullable=False)

    #: 方案内容 + 6 因子分值（17 §10.1~10.3）。NOT NULL：方案没有内容就不是方案。
    payload_json: Mapped[dict] = mapped_column(sa.JSON, nullable=False)

    #: 是否降级 / 降级原因。与 `payload_json` 里同名字段是同一次写入的投影，
    #: 提出来的理由是「降级不静默」要落 DB CHECK（见模块 docstring 第 6 条）。
    degraded: Mapped[bool] = mapped_column(nullable=False, default=False)
    degrade_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)


class Ledger(BaseEntity):
    """台账，三类合一（17 §4.3 / 15 附录A）。**台账只有一套**，且不提供删除接口。

    写入时机：二次确认卡通过后（15 §6.3）—— **未确认不产生台账**。写入即固化：
    表上没有 `updated_at`，也没有任何可被后续步骤回填的列（见模块 docstring 第 8 条）。
    """

    __tablename__ = "ledgers"
    __table_args__ = (
        # 「EXECUTED 后不允许重复写台账」（15 §11.7）的结构层兜底：
        # 网络重试、并发确认、绕过状态机的手工写入，撞到的都是这条。理由见 docstring 第 7 条。
        sa.UniqueConstraint("job_order_id", name="uq_ledgers_job_order_id"),
        sa.CheckConstraint(_DEGRADE_CHECK, name="degrade_reason_required"),
        sa.CheckConstraint(_LEDGER_LOCATION_CHECK, name="location_columns_by_type"),
        sa.CheckConstraint(
            f"source_location_code IS NULL OR {_LOCATION_LEN.format(col='source_location_code')}",
            name="source_location_code_len6",
        ),
        sa.CheckConstraint(
            f"target_location_code IS NULL OR {_LOCATION_LEN.format(col='target_location_code')}",
            name="target_location_code_len6",
        ),
        # design.md 性能目标表：落位写入（SLA ≤200ms）的查询路径（15 附录B 的
        # `GET /api/ledger`）。列序被 tests/models/test_job.py 钉住。
        sa.Index("ix_ledgers_warehouse_order_no", "warehouse_id", "order_no"),
    )

    job_order_id: Mapped[int] = mapped_column(sa.ForeignKey("job_orders.id"), nullable=False)

    #: 台账类型，取值域复用 `job_type`（17 §九⑤）。
    ledger_type: Mapped[LedgerType] = enum_column(LedgerType, name="ledger_type", nullable=False)

    order_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    material_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    material_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)

    #: 生产批号。三类台账都有（15 附录A）；**移库不改批号**，只记录（17 §4.3 表注）。
    batch_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    qty: Mapped[int] = mapped_column(nullable=False)

    #: 源 / 目标库位。哪一类填哪一个由 `_LEDGER_LOCATION_CHECK` 钉住。
    source_location_code: Mapped[str | None] = mapped_column(sa.String(6), nullable=True)
    target_location_code: Mapped[str | None] = mapped_column(sa.String(6), nullable=True)

    #: 拣货路径（巷道序），只属出库台账（15 附录A）。是数组结构 → JSON 列。
    #: D4 的落列表只映射 17 §10 的 6 类结构，未列此列；与 `ImportSession.files_json`
    #: 同一处理：文档要求这组数据在库里，就不是「多加了列」。
    pick_path_json: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)

    #: 写入时的方案（推荐巷道集 / 顺路取顺序 / 目标巷道，15 附录A 三类都 ✅）。
    #: 台账是审计快照，必须自包含 —— 方案表会被后续重规划追加新行，台账不该跟着变。
    plan_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 操作人。NOT NULL —— 15 附录A 三类台账都记操作人；没有操作人的台账无法追溯。
    #: 外键由 §5 的迁移补上（见模块 docstring 第 11 条）。
    operator_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False)

    executed_at: Mapped[datetime] = mapped_column(nullable=False)

    #: 降级标记 / 降级原因（15 附录A：入库与移库有、出库无 —— 故可空且默认未降级）。
    degraded: Mapped[bool] = mapped_column(nullable=False, default=False)
    degrade_reason: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)


class Verification(BaseEntity):
    """后验记录（17 §4.4）。一行一条指标 —— 入库有两条（同物料 / 同批跨巷道，15 §7.1）。

    后验由台账写入成功**自动触发**（15 §6.3），无需人工点按。

    17 §4.4 的「后验时间」即 `created_at`：记录在後验完成的那一刻建，两者是同一个时刻。
    与 §3 对「导入操作时间」的处理一致（那边也没有为同一个时刻再立一列）。
    """

    __tablename__ = "verifications"
    __table_args__ = (
        sa.UniqueConstraint(
            "job_order_id", "metric_kind", name="uq_verifications_job_order_metric_kind"
        ),
    )

    job_order_id: Mapped[int] = mapped_column(sa.ForeignKey("job_orders.id"), nullable=False)

    #: 后验指标名（15 §7.1 的口径名，如「同物料跨巷道」）。
    #: **不建枚举** —— 理由见模块 docstring 第 9 条。
    metric_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 实际值 / 目标阈值。用 Float：17 §10.6 的 KPI 卡片里同物料跨巷道实测值是 **4.8**、
    #: 落位准确率是 0.995 —— 整数列会把它们截断，而截断后的值仍「看着合理」。
    actual_value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    threshold_value: Mapped[float] = mapped_column(sa.Float, nullable=False)

    verify_result: Mapped[VerifyResult] = enum_column(
        VerifyResult, name="verify_result", nullable=False
    )


#: 状态「已发起移库」必须指向那张移库单 —— 见模块 docstring 第 10 条。
_DEVIATION_RELOCATE_CHECK = (
    f"(status <> '{DeviationStatus.RELOCATE_STARTED.value}')"
    " OR (relocate_job_order_id IS NOT NULL)"
)

#: 17 §4.4 的标识是「批次 / **物料**标识」—— 两者至少要有一个。两个都空的偏离
#: 处置不了也复核不了；与 D5 给 `CapAlert` 加「恰好一个来源」CHECK 是同一个理由。
_DEVIATION_IDENTIFIER_CHECK = "(batch_no IS NOT NULL) OR (material_code IS NOT NULL)"


class Deviation(BaseEntity):
    """偏离批次（17 §4.4）。后验超标的批次自动打上偏离标记（15 §7.2）。

    它是**移库作业的任务来源**，也是治理回路的入口：`Verification → Deviation → JobOrder`。
    17 §4.4 的「发现时间」即 `created_at`（与 `Verification` 同一处理）。
    """

    __tablename__ = "deviations"
    __table_args__ = (
        sa.CheckConstraint(_DEVIATION_IDENTIFIER_CHECK, name="identifier_required"),
        sa.CheckConstraint(_DEVIATION_RELOCATE_CHECK, name="relocate_started_needs_job_order"),
    )

    batch_no: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    material_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    #: 实际跨巷道数 / 阈值。**阈值随偏离一并存下**：阈值是可配的（15 §10.6），
    #: 只存实测值的话，配置一改，历史偏离就会被按新阈值重新解读 —— 那正是
    #: 「历史台账不许改写」要防的失真。
    actual_cross_aisle: Mapped[int] = mapped_column(nullable=False)
    threshold_cross_aisle: Mapped[int] = mapped_column(nullable=False)

    cause_kind: Mapped[DeviationCauseKind] = enum_column(
        DeviationCauseKind, name="cause_kind", nullable=False
    )
    status: Mapped[DeviationStatus] = enum_column(
        DeviationStatus, name="deviation_status", nullable=False, default=DeviationStatus.OPEN
    )

    #: 由本偏离发起的移库单。可空（未处理 / 已改善的偏离没有或不再需要它）。
    relocate_job_order_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("job_orders.id"), nullable=True
    )
