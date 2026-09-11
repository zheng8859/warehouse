"""主数据链：仓库 → 巷道 → 库位（6 实体）。

事实来源：17-数据模型设计 §二（主数据实体）、§九（枚举）
          16-数据衔接与 cap 自维护 §4.2（库位编码规则·已实证）、§6.1（巷道总格数来源）、
          附录 A.1/A.2（导入模版字段映射）、A.4（后端派生字段）
          PRD 8.3.1（数据字典 · 库位编码规则 · 真实样本）
          design.md D3（取值约束落两层：Python 枚举 + DB CHECK）、D8（模型改动必带同 commit 迁移）

## 本组的四条例外，各有出处

1. **`Location` 不建到 `Aisle` 的外键。** `16` §1.1 明说「不做主数据管理」，且巷道是库存快照
   解析时由库位号 `[:2]` **派生**的字段（`16` A.4 的 `aisle`）。文档从未要求「库位落库前
   必须先有巷道记录」。
   取而代之，把**编码规则本身**钉进 CHECK：库位号 6 位，且巷道 / 层列 / 格三个两位段
   必须与库位号逐位相符。这样「巷道级聚合对库位号 `[:2]` 切片」这条不变量在库层面就成立，
   不依赖写入方自觉。
2. **`AisleStation` 相反，建复合外键。** 17 ER 图是 `Aisle ||--|| AisleStation`（一对一），
   且它是独立导出的主数据（`17` §2.2 待补充导出），落库时巷道记录必然已在。
3. **`Aisle.total_cells` 与 `Aisle.is_near_station` 可空**，不是偷懒：
   - 两者同源于**待补充导出**的巷道主数据（`16` §6.1）；未导出前 cap 的「总格数」
     以最近一次快照推导的**去重格数**近似（同处），近似值由 cap 计算层回填。
   - 可空用于表达「权威值未到位」。**`is_near_station = NULL` 不得当 `False` 用** ——
     那会让近站台巷道静默退出预留池，正是「降级不静默」（`14` §3.5）要拦的静默失败。
     消费方见到 NULL 应走降级并在 `degrade_reason` 中标注（16 A.4：该因子降级不参与评分）。
4. **`Material.abc_class` 可空**：它由成品清单（历史流水）按料号聚合**自动补录**（`16` A.4），
   物料行可能先于成品清单导入而存在。

## 两处「状态」列不建 CHECK

`Location.状态` 与 `Batch.状态` 在 `17` §2.1 / §2.4 只列了字段名，**全文未给取值域**。
唯一候选是 `item_status`（源数据驱动、以 GTJ10036 导出为准），但没有任何原文把二者等同。
故按文本存储、不建 CHECK —— 与 design.md Open Questions 对 `item_status` 的既有处置一致
（取值由 `FieldMappingConfig` 归一，导入期校验）。**不要把 item_status 的已知取值抄成 CHECK。**
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AbcClass
from app.models.base import BaseEntity, enum_column


class Warehouse(BaseEntity):
    """仓库主数据。`17` §2.1。

    首期单厂，本表只有一行：仓库号 `GTJ10036`，它同时是全部实体的过滤维度 `warehouse_id`
    （`16` A.1 把导入模版的「仓库号」直接映射为 `warehouse_id`；`17` §2.1「仓库号作为
    全部数据的过滤维度」）。

    因此本表的业务键就是继承来的 `warehouse_id`，另加唯一约束 —— 与其它实体把
    `warehouse_id` 当外键维度不同，这里它是主数据本身。
    """

    __tablename__ = "warehouses"
    __table_args__ = (
        sa.UniqueConstraint("warehouse_id", name="uq_warehouses_warehouse_id"),
    )

    #: 仓库名。文档列为字段但未给取值示例，种子写「广州顶津成品库」一类可读名。
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)

    #: 工厂编码。`17` §2.1 列为 Warehouse 字段，但全文未给出与仓库号**区分**的口径
    #: （`19` §3.4 的多厂举例里「单厂编码」就是 `GTJ10036`，与仓库号同值）。
    #: 故可空：为空表示「与 warehouse_id 同值」，不在这里编造第二套编码体系。
    #: 此为待确认项，已登记在 tasks.md 9.4。
    plant_code: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class Aisle(BaseEntity):
    """巷道主数据。`17` §2.1。

    巷道号 = 库位号前 2 位（`010104` → `01`），是 cap 与全部巷道级因子的聚合单位。
    近站台巷道参与预留计算（文档举例为 `01` / `02`，`14` §2.2）。
    """

    __tablename__ = "aisles"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "aisle_no", name="uq_aisles_warehouse_aisle_no"
        ),
        sa.CheckConstraint("length(aisle_no) = 2", name="aisle_no_len2"),
    )

    #: 巷道号：两位文本，即库位号 `[:2]`。存文本 —— 前导 0 不得丢（`01` ≠ `1`）。
    aisle_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 总格数（cap 的计量单位是「格」，CONTEXT.md）。权威值来自巷道主数据导出；
    #: 未导出时由 cap 计算层填快照推导的近似值（`16` §6.1），故可空。
    total_cells: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    #: 是否近站台。**可空 = 巷道主数据未导出**，切勿当 False 用（见模块 docstring 第 3 条）。
    is_near_station: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)


class Location(BaseEntity):
    """库位主数据。`17` §2.1。

    库位号 6 位文本 = 前 2 位巷道 + 中 2 位层列 + 后 2 位格（`16` §4.2「已实证」，
    三处文档一致）。「层列」是一个字段，文档没有把「层」与「列」再拆开
    （`17` §2.1 的字段列表只有「层列」「格」两项）。

    必须按文本读取：Excel 会把 `010104` 数值化成 `10104`，前导 0 一丢，
    巷道切片 `[:2]` 立刻算错。
    """

    __tablename__ = "locations"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "location_code", name="uq_locations_warehouse_location_code"
        ),
        # 编码规则落库（模块 docstring 第 1 条）：三个两位段必须与库位号逐位相符。
        # 单靠写入方自觉，某次导入把巷道切错就会污染全部巷道级聚合，且很难发现。
        sa.CheckConstraint("length(location_code) = 6", name="location_code_len6"),
        sa.CheckConstraint(
            "substr(location_code, 1, 2) = aisle_no", name="location_code_aisle"
        ),
        sa.CheckConstraint(
            "substr(location_code, 3, 2) = layer_column_no",
            name="location_code_layer_column",
        ),
        sa.CheckConstraint(
            "substr(location_code, 5, 2) = cell_no", name="location_code_cell"
        ),
    )

    #: 库位号：6 位文本，如 `010104`。
    location_code: Mapped[str] = mapped_column(sa.String(6), nullable=False)

    #: 所属巷道 = `location_code[:2]`。冗余存一列是为了让巷道级连接/聚合不必每次切片，
    #: CHECK 保证它与库位号一致。**不建外键**（见模块 docstring 第 1 条）。
    aisle_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 层列：库位号中 2 位，如 `010104` 的 `01`。
    layer_column_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 格：库位号后 2 位，如 `010104` 的 `04`。
    cell_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 库位状态。取值域文档未定义（见模块 docstring 末节）—— 按文本存、不建 CHECK。
    #: **注意它与 `item_status` 不是一回事**：后者是库存品质状态（合格/待检/冻结）。
    status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)


class AisleStation(BaseEntity):
    """巷道-站台主数据。`17` §2.2。

    **当前待业务方补充导出**（`16` §1.1、附录 A.4）—— 本表首期为**空表**是预期状态，
    不是漏种。未导出前，6 因子中的「站台就近」**降级不参与评分**，并在推荐理由中标注
    降级原因（`16` A.4 与 `17` §10.1 的 `degraded` / `degrade_reason`）。

    与 `Aisle` 是**一对一**（17 ER 图 `Aisle ||--|| AisleStation`），故唯一键 = 外键列。
    """

    __tablename__ = "aisle_stations"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "aisle_no", name="uq_aisle_stations_warehouse_aisle_no"
        ),
        # 复合外键指向 aisles 的唯一键。父表 (warehouse_id, aisle_no) 有唯一约束，
        # SQLite 在 foreign_keys=ON 下才会真正校验。
        sa.ForeignKeyConstraint(
            ["warehouse_id", "aisle_no"],
            ["aisles.warehouse_id", "aisles.aisle_no"],
        ),
    )

    aisle_no: Mapped[str] = mapped_column(sa.String(2), nullable=False)

    #: 就近出库站台编号，文档举例「站台 A」/「站台 B」（PRD 8.3.1 C 算例）。
    station_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 距离权重：供「站台就近」因子打分，文档举例 0.9（近）/ 0.3（远）。
    #: **不加 0~1 的 CHECK** —— 文档只给了示例值，未声明值域上界；把未声明的口径
    #: 固化成约束，会挡住真实导出（例如业务方给的是米数而非归一化权重）。
    distance_weight: Mapped[float] = mapped_column(sa.Float, nullable=False)


class Material(BaseEntity):
    """物料主数据。`17` §2.3。

    料号是全部推荐与度量的最小粒度（「物料号级」是产品定位），卡片与方案表的展示序为
    单据号 → 物料编码 → 物料描述 → 其他（`15` §9.x）。
    """

    __tablename__ = "materials"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "material_code", name="uq_materials_warehouse_material_code"
        ),
    )

    #: 料号（物料编码），如 `3001234`（`16` A.1）。
    material_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 品名（物料描述），如 `PET500 茉莉柚茶`。`16` A.1 标为**选填**，故可空。
    material_name: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)

    #: ABC 分类：`A` / `B` / `C`（`17` §九）。决定预留池使用权与排序优先级。
    #: 可空 —— 由成品清单聚合**自动补录**（`16` A.4），物料行可能先于它存在。
    #: 消费方见 NULL 应视为「未知」，不得当成 C 类静默放行（预留池红线的保守侧）。
    abc_class: Mapped[AbcClass | None] = enum_column(
        AbcClass, name="abc_class", nullable=True
    )

    #: 箱规：每箱入数。真实样本「PET500茉莉柚茶15入纸箱…102/板」→ 15（PRD 8.3.1）。
    units_per_carton: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    #: 板规：每板箱数。同一样本 → 102。
    #: 箱规/板规用于「数量 → 板数 → 占用格数」折算，换算规则按单厂内置（`17` §2.3、
    #: `16` A.4）。两列可空：文档只给了字段名与一个样本串，未给取值域，也未说明
    #: 单位；空值表示「未导出/未解析」。单位口径为待确认项，已登记在 tasks.md 9.4。
    cartons_per_pallet: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class Batch(BaseEntity):
    """批次主数据。`17` §2.4。

    批号于**入库过账时生成**（PO 不含批号，`16` A.2 / `14` §3.1）；**移库不改批号**，
    仅调整所在巷道。一个料号有多个批次（17 ER 图 `Material ||--o{ Batch`）。
    """

    __tablename__ = "batches"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "batch_no", name="uq_batches_warehouse_batch_no"
        ),
    )

    #: 批号，如 `GJP2571221`（`16` A.1）。
    batch_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 所属物料。用代理键而非料号：料号是业务键，改号时不应级联改批次行。
    material_id: Mapped[int] = mapped_column(
        sa.ForeignKey("materials.id"), nullable=False
    )

    #: 生产日期。`16` A.2 标为**选填**，供 FIFO 与批次生成参考，故可空。
    #: 类型为日期而非时间戳 —— 源文件的 Excel 序列号须先转日期（CLAUDE.md 红线）。
    production_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)

    #: 批次状态。取值域文档未给（见模块 docstring 末节）—— 按文本存、不建 CHECK。
    status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
