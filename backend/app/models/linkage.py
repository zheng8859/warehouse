"""衔接链 5 实体：导入会话 → 快照基线 → 库存分布 / 巷道容量 / 容量告警。

事实来源：17-数据模型设计 §三（数据衔接实体）、§10.4 / §10.5（回执与 cap 快照 JSON）
          16-数据衔接与 cap 自维护 §3（状态机与 ImportSession 实体）、§6.1~6.5（cap 与告警）
          openspec/changes/data-model-permission/design.md D1 / D2 / D4 / D5 / D8
          spec `data-model`「不可变快照基线与 cap 引用」「乐观锁并发守卫」「枚举登记范围」

实体：ImportSession(导入会话) / Snapshot(快照基线) / InventoryItem(库存分布)
      AisleCap(巷道容量) / CapAlert(容量告警)

## 关键口径

- `cap_total`    = 巷道总格数 − 已占格数
- `cap_reserved` = `cap_total` × 近站台预留比例（默认 40%，仅近站台巷道非零）
- `cap_usable`   = `cap_total` − `cap_reserved`（非 A 类可用）
- 快照是权威、增量是过程：每次快照导入按巷道全量重算并生成新版本，旧版**归档不删除**
  （17 §3.2）。本模块用「`Snapshot` 只能追加」表达这条：表上没有 `updated_at`，
  写入即固化；旧行随时可读。

三列 cap 值的口径只写在列注释里，**不落算术 CHECK**（D5：本阶段只保证列存在）。
算术约束在这里会与「以快照重算值校正漂移」（16 §6.4）冲突 —— 校正后的值不必满足
某条写死的等式，硬钉住只会让校正写不进去。

## 本模块的六处取舍（文档没直说，评审要看的就是这几处）

1. **`ImportSession` 的「业务版本」列 = `snapshot_version_no`**。spec 要求乐观锁与业务版本
   分列，而 17 §3.1 逐项列出的版本类字段只有「乐观锁版本」与「批次号」。本实现取
   16 §3.3「分流去向：快照 → **cap 基线版本号**」这一项作为业务版本列：它与 `lock_version`
   语义不同、可空（会话可在建基准前 `DISCARDED` / `FAILED`），权威值仍是 `Snapshot.version_no`，
   本列只是会话侧的去向记录。
2. **`ImportSession.import_batch_no` 不加唯一约束**。16 §3.1 的状态机允许 `FAILED → DRAFT`
   重新校验后再导入，即一次会话可产生多个批次（16 A.4：一次导入生成一个批次 ID）——
   一对话是未经确认的假设，加唯一约束会在重试路径上误伤。`session_no` 才是会话标识，它唯一。
3. **`InventoryItem` / `AisleCap` 不建指向主数据（`locations` / `batches` / `materials` /
   `aisles`）的外键**。快照必须能记录「源文件里有、主数据还没建」的行：16 A.5 #4 把料号匹配
   列为**校验规则**而非落库前置；巷道与库位本就由快照派生（16 A.4「巷道 = 库位号[:2]」）。
   建外键会形成「先有主数据、才有快照，而主数据又由快照派生」的循环依赖。
   唯一键落在业务列上，与 16 A.4 的「库存行标识 = 库位号 + 批号（+ 料号）」逐字一致。
4. **`CapAlert.ledger_txn_id` 的外键是分两步建的**（D5 要求它是外键，指向台账事务号）。
   外键目标 `ledgers` 表属作业链（`models/job.py`），而 SQLAlchemy 的 `ForeignKey`
   目标表必须已在 `Base.metadata` 中 —— 否则 `create_all` 与迁移在编译 DDL 时就抛
   `NoReferencedTableError`，本组测试与迁移全部跑不起来。故 §3 先落整数列，**§4 落地
   `ledgers` 后由迁移 `f01b0406d12c` 用 `batch_alter_table` 补上**（SQLite 改约束必须走
   batch 重建）。此前 §3 欠的账由 `tests/logic/test_cap_alert.py` 收紧的用例看住
   （断言从「方向」改成等号，并新增一条真外键的负例）；tasks.md 9.4b 已据此销账。
5. **`InventoryItem.production_date` 保留但可空**。17 §3.3 的字段表列了它，而 16 A.1 的 INV
   模版已把它移除（「库区号、生产日期不再需要」）。本表按 17 的字段清单保留列（17 是实体字段
   的单一事实来源），按 A.1 的实际可得性置为可空。
   同组的 `InventoryItem.zone` **已于 `retire-zone-column` 移除**：它的两处文档来源（17 §3.3 的
   字段枚举、16 的模版）都已清退，且全仓从无读写方（无 DTO 字段、无 importer 映射、无因子读它）——
   留一个无来源又无消费方的列，只会让「实体字段以 17 为准」这条口径持续失真。
6. **时间戳按「解析起止 + 里程碑」分列**。16 §3.3 给了四个里程碑（创建 / 校验完成 /
   导入完成 / 建基准完成），design.md 的性能目标表另要求「`ImportSession` 记录解析起止时间
   以支撑解析 ≤5min/文件 的 SLA」。故除 `created_at` 外另有 `validating_at`（解析起点）
   与 `validated_at`（解析终点）等四个可空里程碑列。17 §3.1 的「导入操作时间」即
   `created_at`（会话创建 = 文件已选 + 时点已标注，16 §3.2）—— 文档未给二者差别的口径，
   不另立第二个时刻列。
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ImportStatus
from app.models.base import BaseEntity, enum_column, utcnow


class AlertKind(str, Enum):
    """容量告警类型（16 §6.5 的四个异常）。

    **实体局部值域**：不进 `app/core/enums.py`（17 §九 只登记跨模块共享的 11 个，
    design.md D2）。取值即 17 §3.4 / D5 给出的中文原文 —— 与 `ItemStatus` 同一处理
    （枚举成员名 ASCII、取值照文档原样），因为这三类值会直接出现在回执与看板的
    「告警项」里，翻译成代号反而多一层映射。

    文档内部有一处空格差异：17 §3.4 写「负 cap」、D5 写「负cap」。取 D5（本变更的
    决策记录，且是落进 CHECK 的那个字面量）。
    """

    NEGATIVE_CAP = "负cap"
    OVER_TOTAL_CELLS = "超总格"
    INCREMENT_FAILED = "增量失败"
    DRIFT_EXCEEDED = "漂移超阈值"


class ImportSession(BaseEntity):
    """一次导入批次的完整生命周期（17 §3.1、16 §3.3）。

    状态机见 16 §3.1：`DRAFT → VALIDATING → VALIDATED/FAILED → IMPORTING → IMPORTED
    → BASELINE`，另有 `DRAFT/VALIDATED → DISCARDED`。
    """

    __tablename__ = "import_sessions"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "session_no", name="uq_import_sessions_warehouse_session_no"
        ),
    )

    #: 导入会话 ID，形如 `IMP-20260908-01`（17 §10.4 回执里的 `session_id`）。
    session_no: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 批次号：一次导入生成一个批次，便于整体追溯（16 §3.3 / A.4「导入批次 ID」）。
    #: 不加唯一约束 —— 理由是模块 docstring 第 2 条。
    import_batch_no: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 数据时点（必填，16 §10.6「缺失策略 = 拒绝导入」）。
    #: 用 DATETIME 而非 DATE：模版把它映射到 INV 的「库存记录时间」（16 A.1，类型
    #: 「日期时间」、示例 `2026-09-08 24:00`）。只给日期的源值解析到午夜不丢信息，
    #: 反过来把「带时刻」的值塞进 DATE 列则会静默截断 —— 与 17 §10.4 的 `data_time`
    #: 展示成 `2026-09-08` 并不矛盾，那是渲染形状。
    data_time: Mapped[datetime] = mapped_column(nullable=False)

    #: 三类文件清单（文件名 / 格式 / 大小 / 编码 / 校验和），16 §3.3、17 §3.1。
    #: D4 的 JSON 落列表未列它（那张表只映射 17 §10 的 6 类结构），但实体章节要求
    #: 这组数据在库里 —— 校验和正是 16 §11.5「重复导入」判重的依据，不能只活在日志里。
    files_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 导入校验回执（17 §10.4 的 JSON，D4 落列），**含分流去向**。
    receipt_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 会话状态，取值限 8 个（17 §九③）。
    status: Mapped[ImportStatus] = enum_column(
        ImportStatus, name="import_status", nullable=False, default=ImportStatus.DRAFT
    )

    #: 本次导入产出的 cap 基线版本号 —— 即本会话的**业务版本**列（模块 docstring 第 1 条）。
    #: 可空：会话可能在建基准前就 FAILED / DISCARDED。权威值在 `Snapshot.version_no`。
    snapshot_version_no: Mapped[int | None] = mapped_column(nullable=True)

    #: 乐观锁版本号（D1：与业务版本分列，对用户不可见），守卫见 `app/core/concurrency.py`。
    #: 16 §10.6 的「并发控制：乐观锁版本号」用于防止多端同时导入同一时点。
    lock_version: Mapped[int] = mapped_column(nullable=False, default=0)

    # 解析起止 + 三个里程碑（模块 docstring 第 6 条）。未到达的里程碑为 NULL ——
    # 不填 0 或哨兵时间，否则「解析耗时」会算出负数或天文数字。
    #: 进入 VALIDATING 的时刻（解析起点，供 SLA 度量）。
    validating_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: 校验完成时刻（解析终点）。
    validated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: 写入与分流完成时刻。
    imported_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: cap 基线建完时刻（终态 BASELINE 的到达时间）。
    baselined_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Snapshot(BaseEntity):
    """快照基线（17 §3.2）。**追加式**：新版本覆盖为「旧版归档保留」。

    AS-IS 结构上只有一条防线：本表没有 `updated_at`，写入即固化，历史行随时可读。
    真正的写入路径约束（只 INSERT 不 UPDATE）在阶段四的 cap 计算层，本阶段把
    「旧行不被覆盖」钉成用例（spec `data-model`「新快照不覆盖旧基线」）。
    """

    __tablename__ = "snapshots"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "version_no", name="uq_snapshots_warehouse_version_no"
        ),
    )

    #: 数据时点。一个时点一个版本（16 A.4「以『数据时点』生成新版本」）。
    snapshot_time: Mapped[datetime] = mapped_column(nullable=False)

    #: 版本号，按仓库单调递增。**不放 17 §10.5 的 `"2026-09-08T00:00"` 时间戳串**
    #: （D1）：那是展示形状；存储层用 `Snapshot.id` 作被引用键，时间戳串没有唯一约束，
    #: 且改基线时间会级联污染引用。
    version_no: Mapped[int] = mapped_column(nullable=False)

    #: 按巷道聚合的 cap（17 §10.5 的 JSON，D4 落列）。明细在 `AisleCap` 行里，
    #: 本列是导入当时冻结的那一份视图 —— 两者同事务写入，值必须一致。
    cap_snapshot_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 来源导入会话（17 §3.2）。「按库位/批次的库存分布指针」即 `InventoryItem.snapshot_id`，
    #: 方向在本表这一侧，不另立反向指针列。
    import_session_id: Mapped[int] = mapped_column(
        sa.ForeignKey("import_sessions.id"), nullable=False
    )


class InventoryItem(BaseEntity):
    """库存分布（快照明细，17 §3.3）。字段取自实证的「库存记录表」。

    用途：既有库位（连续性因子）、批次（批次因子）、cap 聚合、出库顺路取的落位来源。
    """

    __tablename__ = "inventory_items"
    __table_args__ = (
        # 唯一键 = 库位号 + 批号 + 料号（16 A.4「库存行标识」）。快照维度打头：
        # 「唯一」是在**一份快照内**唯一，不同版本的同一条库存各占一行 —— 这正是
        # 快照能供追溯与趋势分析的前提（17 §12）。warehouse_id 按项目约定一并入键。
        sa.UniqueConstraint(
            "warehouse_id",
            "snapshot_id",
            "location_code",
            "batch_no",
            "material_code",
            name="uq_inventory_items_snapshot_location_batch_material",
        ),
        # 库位号 6 位文本（CLAUDE.md §七）。前 2 位即巷道，故长度错了巷道聚合就错。
        sa.CheckConstraint("length(location_code) = 6", name="location_code_len6"),
        # 数量 > 0 是 16 A.1 写明的校验规则（"数量按整数存" 的理由见字段注释）。
        sa.CheckConstraint("qty > 0", name="qty_positive"),
    )

    #: 所属快照版本。**不建到 `locations` / `batches` / `materials` 的外键**（模块
    #: docstring 第 3 条）。
    snapshot_id: Mapped[int] = mapped_column(
        sa.ForeignKey("snapshots.id"), nullable=False
    )

    #: 库位号，6 位文本 —— 前导 0 不得丢（Excel 数值化会丢，CLAUDE.md §七）。
    location_code: Mapped[str] = mapped_column(sa.String(6), nullable=False)

    material_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 品名（16 A.1 标选填）。快照是源文件行的忠实副本，故冗余保留。
    material_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)

    batch_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 生产日期。17 §3.3 列了它、16 A.1 的 INV 模版已移除 → 可空（模块 docstring 第 5 条）。
    production_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)

    #: 库存品质状态（16 A.1 的「状态」）。**不建 CHECK**：取值域以 GTJ10036 导出为准，
    #: 由 `FieldMappingConfig` 归一（design.md Open Questions / 17 §9⑪）。
    item_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 数量。按整数存：16 A.1 / A.2 / A.3 的样本全是整数（40 / 500 / 120），且它要按
    #: 「板-格」换算规则折算成占用格数（16 A.4）。真实导出若含小数，须先改 17 再改列类型。
    qty: Mapped[int] = mapped_column(nullable=False)

    #: 库存记录时间（16 A.1 第 10 行，映射名即 `snapshot_time`）。
    #: 与 `ImportSession.data_time` 是同一个时点，口径见 16 §七「数据时点须与标注时点一致」；
    #: 两处列名不同是因为来源不同（会话侧是用户标注、明细侧是文件字段），不是两个概念。
    snapshot_time: Mapped[datetime] = mapped_column(nullable=False)


class AisleCap(BaseEntity):
    """巷道容量（17 §3.4）—— 全量重算的产物，一行/巷道/快照（D5）。

    「事务内增量」**不落本表**：它靠 `Ledger` 同事务更新，台账是 cap 增量的唯一来源
    （16 §6.3）。本表只承载快照时刻的冻结值，故 `snapshot_id` 必填。
    """

    __tablename__ = "aisle_caps"
    __table_args__ = (
        # 一行/巷道/快照。这个唯一约束同时是 design.md 性能目标表要的
        # `(warehouse_id, snapshot_id, aisle)` 索引 —— 前缀正好覆盖 cap 查询（SLA ≤100ms），
        # 不另建重复索引。
        sa.UniqueConstraint(
            "warehouse_id",
            "snapshot_id",
            "aisle_no",
            name="uq_aisle_caps_snapshot_aisle_no",
        ),
        sa.CheckConstraint("length(aisle_no) = 2", name="aisle_no_len2"),
    )

    #: 来源基线。**不建到 `aisles` 的外键**（模块 docstring 第 3 条）。
    snapshot_id: Mapped[int] = mapped_column(
        sa.ForeignKey("snapshots.id"), nullable=False
    )

    aisle_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: cap_total = 巷道总格数 − 已占格数（17 §3.4 注）。口径不落 CHECK（模块 docstring）。
    cap_total: Mapped[int] = mapped_column(nullable=False)
    #: cap_reserved = cap_total × 预留比例（默认 40%，仅近站台巷道非零）。
    cap_reserved: Mapped[int] = mapped_column(nullable=False)
    #: cap_usable = cap_total − cap_reserved（非 A 类可用）。
    cap_usable: Mapped[int] = mapped_column(nullable=False)

    #: 是否近站台。与 `Aisle.is_near_station` 同样**可空**：取值随快照冻结，
    #: 巷道主数据未导出时为空 —— NULL 表示「未导出」，不得当 `False` 用。
    is_near_station: Mapped[bool | None] = mapped_column(nullable=True)

    #: 更新时间（17 §3.4 的字段表）。本表是快照产物、正常只写一次，但漂移校正
    #: （16 §6.4「以快照重算值为准校正」）会改写同一行，故需要它。
    #: 用 Python 侧 `utcnow` 而非数据库函数：SQLite 没有 `utcnow()`，且 base.py 已把
    #: 「时间列统一存 naive UTC」定成口径，两条要一致。
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=utcnow, onupdate=utcnow
    )


class CapAlert(BaseEntity):
    """容量告警（17 §3.4、16 §6.5）。

    引用**恰好一个**来源（D5）：要么指向快照（全量重算发现的负 cap / 超总格 / 漂移），
    要么指向台账事务（事务内增量失败）。两个都空 = 无法定位的告警，两个都非空 =
    不知道该以谁为准 —— 两种都写成 CHECK 拦在库层，不靠调用方自觉。
    """

    __tablename__ = "cap_alerts"
    __table_args__ = (
        sa.CheckConstraint(
            "(snapshot_id IS NULL) <> (ledger_txn_id IS NULL)",
            name="alert_source_exactly_one",
        ),
        sa.CheckConstraint("length(aisle_no) = 2", name="aisle_no_len2"),
    )

    #: 告警类型（实体局部值域，见 `AlertKind`）。
    alert_kind: Mapped[AlertKind] = enum_column(AlertKind, name="alert_kind", nullable=False)

    #: 巷道号。四类告警都是巷道级的（16 §6.5：负 cap / 超总格 判定在巷道，
    #: 增量失败与漂移也在某一巷道上），故必填。
    aisle_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 来源之一：快照基线。null 表示这条告警来自台账事务。
    snapshot_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("snapshots.id"), nullable=True
    )

    #: 来源之二：台账事务号（D5 要求它是外键）。
    #: §3 落地本表时 `ledgers` 还不存在 —— SQLAlchemy 的 `ForeignKey` 目标表必须已在
    #: `Base.metadata` 中，先声明会让 `create_all` 与迁移在编译 DDL 时抛
    #: `NoReferencedTableError`（本表的测试与建表当时全跑不起来）。故 §3 先落整数列，
    #: 这条外键由迁移 `f01b0406d12c`（作业链，与本节同名）用 `batch_alter_table` 补上
    #: —— SQLite 改约束只能 batch 重建。
    ledger_txn_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("ledgers.id"), nullable=True
    )

    #: 详情（异常明细：文件 + 列 + 行 + 原因，16 §3.2 的 FAILED 行）。必填 ——
    #: 告警的消费方是「能不能处置」，没有详情的告警处置不了。
    detail: Mapped[str] = mapped_column(sa.Text, nullable=False)

    #: 是否已处理。D2 把它与 `alert_kind` 一并归为实体局部值域（不进 enums.py）——
    #: 它是布尔列，不是枚举列，但同属「只在本实体内有意义」的取值。
    handled: Mapped[bool] = mapped_column(nullable=False, default=False)
