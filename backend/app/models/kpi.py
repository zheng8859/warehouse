"""KPI 实体（1 个）。

事实来源：17-数据模型设计 §五（度量与 KPI 实体）、§10.6（KPI 卡片 JSON）、§十二（留存）
          18-KPI 与验收度量设计 §3.1（指标快照状态机）、§3.2（状态详解）、§3.3（实体）、
          §4.1（三层聚合）、§「阈值与口径变更须记录口径版本」
          design.md D1（版本语义）、D2（枚举落点）、D3（取值约束落两层）、D4（JSON 列）
  KpiSnapshot(指标快照)

统计周期：周 / 月。指标口径见 18 号 —— 统一验收指标 = 拣货量加权集中度
（80% 拣货量落在 ≤N 巷道，默认 N=5）。

## 本实体的四处取舍

1. **一仓库一周期一行**（唯一键 `(warehouse_id, period)`）。18 §3.1 的状态机里
   `READY → STALE → PENDING` 与 `ERROR → PENDING` 都是**同一行**的状态变化（重算），
   不是新增行 —— 若同期可多行，看板「查最新」就会取到随机的一行。这个唯一键同时是
   18 §「前端查询 `/api/kpi/snapshot` 取最新」的索引（列序 warehouse_id 打头），
   与 `AisleCap` 同一处置：唯一约束兼作查询索引，不另建重复索引。
2. **指标列全部可空**，这不是宽松，是状态机的要求：18 §3.2 逐行写明 `PENDING` 时
   「可获取的信息 = 周期、仓库号」，`ERROR` 时只有错误描述。若写成 NOT NULL，
   状态机在存储层直接落不了地。同理 `card_json` 可空。
3. **`status` 来自 18 §3.1，17 §5.1 的字段表没有它**。这是两处文档的**互补**而非冲突：
   17 定「存什么字段」，18 定「这些字段经历什么状态」。落地这一列的理由是 design.md
   的 Goals 明写「让阶段三~七能直接依赖字段名与枚举取值，不必回头改模型」——
   状态机属阶段六的 KPI 实现，缺列就得再补一次迁移。已登记在 tasks.md 9.4e①。
4. **「环比」落 JSON 列**。17 §5.1 的字段表列了它，但 17 与 18 全文**都没有口径**
   （是哪个指标的环比、单值还是分指标各一个）。用单值列等于替文档定口径，
   故落 JSON 能装下任何口径，待 18 补口径后再收紧成数值列 + 迁移（登记在 9.4e②）。
   另：`18` §「历史 KpiSnapshot 保留旧口径，趋势对比须同口径」要求的**口径版本引用**
   本阶段不落列 —— 目标 `CapacityConfig` 属 §6（尚未建模），同 §3/§4 的延期外键处理，
   登记在 9.4e③。

KpiSnapshot 是**周期快照**（18 §4.1 的确定性三层聚合产物），与台账同为不可改写的
历史：18 §「历史 KpiSnapshot 保留旧口径」要求趋势对比不被口径变更污染。本表因此
没有 `updated_at` —— 重算改的是 `status` 与指标值（同一行的状态机循环），
「这一行何时生成」由继承来的 `created_at` 回答（17 §5.1 的「生成时间」即它，
不另立第二列，与 `ImportSession` 同一处置）。
"""
from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseEntity, enum_column


class KpiStatus(str, Enum):
    """指标快照状态（18 §3.1 的状态机）。

    **实体局部值域**：不进 `app/core/enums.py` —— 17 §九 只登记跨模块共享的 11 个
    （design.md D2），而本值域只在本实体内有意义。D2 逐条列出的四处局部值域来自
    17 的实体字段表，本处来自 18 的状态机，是同一条口径的第五处落点。

    取值即状态名（成员名 ASCII、取值照文档），与 `AlertKind` 同一处理：它们会直接
    出现在看板的「数据过期」标注里（18 §「看板展示上一次 READY 值并标注数据过期」），
    翻译成代号只多一层映射点。

    状态机本体（迁移表与守卫）属阶段六的 KPI 计算层 —— 18 §3.1 的图这里不重复实现，
    本阶段只把取值与默认值定死，让那一层不必回头改模型。
    """

    PENDING = "PENDING"
    COMPUTING = "COMPUTING"
    READY = "READY"
    STALE = "STALE"
    ERROR = "ERROR"


class KpiSnapshot(BaseEntity):
    """指标快照（`17` §5.1、`18` §3.3）。

    一行 = 一个仓库一个统计周期的全部指标（模块 docstring 第 1 条）。
    """

    __tablename__ = "kpi_snapshots"
    __table_args__ = (
        # 列序即索引（模块 docstring 第 1 条）。
        sa.UniqueConstraint(
            "warehouse_id", "period", name="uq_kpi_snapshots_warehouse_period"
        ),
    )

    #: 统计周期，形如 `2026-W37`（17 §10.6 的示例）或月度形式。长度 16 足够放下
    #: `2026-W37` / `2026-09` 两类写法 —— 具体格式由 18 的生成侧决定，本阶段只约束宽度。
    #: **它是业务键而非时间戳**：`created_at` 是「何时算的」（重算会变），
    #: `period` 是「算的哪一周」（不变），用前者当键重算一次就多一行（D1 的同一口径）。
    period: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    #: 快照状态（模块 docstring 第 3 条）。默认 `PENDING`：18 §3.1 的入口状态。
    #: 默认为 `READY` 会让计算失败的行被看板当成就绪数据展示 —— 正是 18 §
    #: 「不得静默显示空或错值」要拦的。
    status: Mapped[KpiStatus] = enum_column(
        KpiStatus, name="kpi_status", nullable=False, default=KpiStatus.PENDING
    )

    #: 拣货量加权集中度：对一张 DO 按拣货量降序累加至 **80%** 时覆盖的巷道数
    #: （17 §5.1 / 18 §1.3，单位验收指标）。取周期内均值，故是 Float ——
    #: 17 §10.6 的示例正好是整数 4，但那是**一周的样本**，不是列类型。
    #: 阈值 N（默认 5）不在此列：它属 `CapacityConfig`（17 §七 的「加权集中度 N」），
    #: 卡片 JSON 里带上的是**当次生效**的那一份（见 9.4e③）。
    weighted_concentration: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    #: 同物料跨巷道数均值（验收 ≤5，17 §10.6 示例 4.8）。
    same_material_cross_aisle: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    #: 同批跨巷道数均值（验收 ≤3，17 §10.6 示例 3.2）。
    same_batch_cross_aisle: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    #: 推荐采纳率（验收 ≥60%，17 §10.6 示例 0.68）。比例存 0~1 而非百分数：
    #: 与文档示例一致，避免「68 还是 0.68」的歧义在库里流转。
    #: **无样本时为 NULL 而不是 0** —— 0% 采纳率看起来像全线崩盘，
    #: 而真实语义是「本周还没有可采纳的决策」（与 `Aisle.is_near_station` 同一处置：
    #: 0 / False 是一个具体的值，不能拿来表示「没有值」）。
    adoption_rate: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    #: 落位准确率（验收 ≥99%，17 §10.6 示例 0.995）。同样可空、同样不是 0。
    placement_accuracy: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    #: 环比（模块 docstring 第 4 条）。首期为 NULL —— 上一期不存在。
    period_over_period_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: KPI 卡片（17 §10.6 的 JSON，D4 落列）。承载「各指标值与阈值、PASS/DEVIATION
    #: 判定」的展示形状 —— 与上面的散列是同一批数的两种形态：散列供查询与聚合，
    #: 卡片供看板直出（18 §「前端查询 /api/kpi/snapshot」）。两个都要，
    #: 是因为看板还需要阈值与判定，而阈值属配置、判定属计算层，不该各建一列。
    #: 可空：`PENDING` / `ERROR` 的行还没有卡片（模块 docstring 第 2 条）。
    card_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
