"""配置与对话台实体（4 + 1）。

事实来源：17-数据模型设计 §七（配置实体：权重 / 容量与阈值 / 字段映射 / 问句模板）、
          §八（对话台上下文）、§九（枚举）、§10.1（推荐理由的 6 因子权重示例）、
          §十一（数据隔离）、§十二（留存）
          14-推荐引擎与评分流程设计 §四（6 因子集合固定）、§3.3（近站台预留比例）、
          §3.5（预留超时释放）
          16-数据衔接与 cap 自维护 §6.1 / §353~356（阈值默认值表）、附录 A.1（字段模版）
          15-入库出库移库与后验流程设计 §8.2（四类可点击问句）、§8.3（参数化模板）、
          §8.5（新增一条问句即新增一条映射）
          10-AI 辅助能力（冷路径）设计 §152（四类冷路径能力）
          design.md D1（版本语义三分）、D2（枚举落点）、D3（取值约束落两层）、
          D4（JSON 列）、D8（迁移与模型同提交）
  WeightConfig(权重配置) / CapacityConfig(容量与阈值配置)
  FieldMappingConfig(字段映射，GTJ10036 内置) / PromptTemplate(问句模板)
  ConversationContext(对话台上下文)

关键口径：
  - 配置全量版本保留、可回滚；权重变更后下次评分生效
  - CapacityConfig 默认值：预留比例 40%、超时释放 18:00、加权集中度 N=5、
    同物料跨巷道 ≤5、同批跨巷道 ≤3
  - 字段映射首期内置，不做可视化配置（v0.11 决策）

命名说明：本模块是「领域配置实体」，与 app/core/config.py（应用运行配置）无关，
故命名为 configuration.py 以避免混淆。

## 本组的取舍

1. **只有两张配置表带版本**（`WeightConfig` / `CapacityConfig`），字段映射与问句模板
   不带。这不是漏了，是文档的分层：17 §七 把版本号 / 生效时间 / 变更人**只列在**
   权重配置上，容量配置在 17 §七 的字段表里同样有这三项（其默认值与阈值来自 16），
   而字段映射写明「按单厂编码内置」、问句模板走 15 §8.5 的「新增一条映射即生效」。
   版本机制是为了**可回滚的口径变更**；内置数据与可增删的映射没有「回滚到上一版」
   的语义（改错了就再改一条），给它们加版本只会多出一堆永远没人读的历史行。
2. **权重落六列而不是一个 JSON**。14 §四 / §135 明说 6 因子「不新增、不删除」——
   集合固定时，六列能逐列加约束、能按因子查询、能被 `ALTER TABLE` 单独改；
   JSON 则把「因子少了一个」这类错误推迟到运行期才发现（D4 只把 17 §10 的
   **6 类结构**落成 JSON 列，权重不属其中）。
3. **权重只 CHECK「每项在 [0,1]」，不 CHECK「和为 1」**。六项权重之和是否为 1，
   文档全文没有口径（17 §10.1 只是给了一组恰好和为 1.00 的**示例值**）。加这条 CHECK
   等于替文档定口径：将来若要支持「一项权重故意留空、由其余项归一」或「允许临时
   放大某一项看重排效果」，约束会先于需求把路堵死。已登记 tasks.md 9.4f。
4. **`reserved_release_at` 是 `Time`（当日钟点），不是 `DateTime`**。16 §353~356 写的是
   「当日 18:00 释放」—— 每天重复发生，没有日期分量。落成 `DateTime` 就必须补一个
   「用哪天的日期」的口径（今天？导入日？），而那个口径文档没给。也正因如此，
   **它是本地墙上时间（wall-clock），不是 naive UTC** —— 与其余时间戳列相反：
   18:00 指「现场的 18:00」，若按 UTC 存再按 UTC 比，现场会变成次日 02:00 释放。
5. **对话台的 `pending_write_json` 落上下文，不建确认卡实体**。确认卡是前端交互态
   （15 §7.2），落库的只有「待确认的写操作上下文」——否则会与 `JobOrder` 上的
   `disposition` / `confirm_card_digest` 形成两套处置记录（红线「台账只有一套」同理）。
6. **`ConversationContext` 没有 `updated_at`**，尽管每轮对话都会追加。17 §八 的字段表
   只给「创建·过期时间」，而 17 §十二 的滑动续期（会话级 ≤8 小时 / 关闭即清除）
   只需**重写 `expires_at`** 就能表达。多一列 `updated_at` 会让人以为「最后一次问句
   的时间」存在那里，而现场真正要用的判断始终是「过期了没有」。
"""
from __future__ import annotations

from datetime import datetime, time
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import FileType
from app.models.base import BaseEntity, enum_column

#: 6 因子权重列的后缀，顺序即 17 §10.1 推荐理由里的列举顺序。
#: 14 §四 钉死「不新增、不删除」，故这里是常量而不是可扩展结构。
WEIGHT_FACTORS: tuple[str, ...] = ("abc", "cap", "existing", "station", "batch", "continuity")


class Capability(str, Enum):
    """冷路径能力（17 §七 的「映射的冷路径能力」）。

    **实体局部值域**：不进 `app/core/enums.py`（D2）。它是本阶段的第 6 处局部值域
    （D2 列了四处，第五处是 18 §3.1 的 KPI 状态）。

    成员名 ASCII、**取值照文档**（10 §152 / 15 §8.2 用中文列举）：取值与问句模板一起
    维护，将来在管理页面上是直接展示给人看的（15 §8.5「新增一条问句即新增一条映射，
    无需改代码」），翻成代号只多一层要维护的映射，且埋下一处「页面显示的是什么」的歧义。
    """

    KPI_DIGEST = "KPI 解读"
    DEVIATION_ATTRIBUTION = "偏离归因"
    WEIGHT_TUNING = "权重调优"
    RELOCATE_PLAN = "移库方案"


class WeightConfig(BaseEntity):
    """6 因子权重配置（`17` §七 / §10.1）。

    一行 = 一版权重。版本同存、可回滚（§七「历史版本保留可回滚」），
    「当前用哪版」由 `app/core/config_version.py` 现算，**没有指针列**（D1）。
    """

    __tablename__ = "weight_configs"
    __table_args__ = (
        # 版本号在一个仓库内不重号 —— 否则「取 version_no 最大者」会取到两行，
        # 而两行权重不同时，选哪一行会让评分结果不可复现（红线「同样输入必得同样输出」）。
        sa.UniqueConstraint(
            "warehouse_id", "version_no", name="uq_weight_configs_warehouse_version"
        ),
        # 模块 docstring 第 3 条的**下界**部分：每项权重是 0~1 的比例。
        # 1.5 的权重让得分可以超过 1，而 17 §10.1 的示例分值（0.86 / 0.81）在 [0,1] 内。
        # 命名需显式给出：SQLite 的 batch 迁移按名字寻址约束（D8）。
        *[
            sa.CheckConstraint(
                f"weight_{factor} BETWEEN 0.0 AND 1.0", name=f"weight_{factor}_range"
            )
            for factor in WEIGHT_FACTORS
        ],
    )

    #: 业务版本号，从 1 起递增（D1 的配置型版本）。**不是乐观锁** ——
    #: `lock_version` 只给 `JobOrder` / `ImportSession`（D1）。
    version_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    #: 生效时间（naive UTC，与 `created_at` 同一口径）。**可预约生效**：
    #: 时间未到即不参与选取（`is_effective`），故它是「什么时候开始能用」，
    #: 而不是「什么时候写的」—— 后者由 `created_at` 回答。
    effective_at: Mapped[datetime] = mapped_column(nullable=False)

    #: 变更人（17 §七）。**可空**：首版权重由种子写入，没有变更人；沿用
    #: `Account.created_by_id` 的同一处置 —— 不自造一个 `system` 账号来占位。
    #: 自举完成、权重真正由人调整之后，这一列才有值。
    changed_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("accounts.id"), nullable=True
    )

    #: ABC 分类因子权重（17 §10.1 示例 0.25）。
    weight_abc: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: 巷道 cap 因子权重（示例 0.20）。与 `AisleCap` 的关系：cap 是**约束**（能不能放），
    #: 这项是**打分**（放得好不好），两者不是同一件事，故这里存的是权重而非 cap 值。
    weight_cap: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: 既有库位因子权重（示例 0.15）。
    weight_existing: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: 站台就近因子权重（示例 0.20）。
    weight_station: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: 批次连续性因子权重（示例 0.10）。
    weight_batch: Mapped[float] = mapped_column(sa.Float, nullable=False)

    #: 库位连续性因子权重（示例 0.10）。与批次因子分开：14 §四 把「同批归拢」与
    #: 「位置连续」列为两个因子，合并会让「同批但分散」与「连续但不同批」得到同一个分。
    weight_continuity: Mapped[float] = mapped_column(sa.Float, nullable=False)


class CapacityConfig(BaseEntity):
    """容量与阈值配置（`17` §七 / `16` §353~356）。

    一行 = 一版容量口径。默认值即 16 §353~356 的表值 —— 给 Python 侧默认值的理由是
    「配置缺席时按文档默认跑」，而不是「随代码猜一个数」：这六项每一项都出现在
    16 的表里，没有一个是我们补的。
    """

    __tablename__ = "capacity_configs"
    __table_args__ = (
        sa.UniqueConstraint(
            "warehouse_id", "version_no", name="uq_capacity_configs_warehouse_version"
        ),
        # 比例在 [0,1]：`cap_reserved = cap_total × 比例`（14 §3.3），比例 > 1 会让预留池
        # 比巷道容量还大 → `cap_usable` 为负，而一个负的可用容量会静默通过所有
        # 「cap 是否足够」的判断（这正是 `cap` 自维护里最贵的一类错）。
        sa.CheckConstraint(
            "near_station_reserved_ratio BETWEEN 0.0 AND 1.0",
            name="near_station_reserved_ratio_range",
        ),
        # 三项计数阈值 ≥ 1：阈值 0 会让任何一行都「超标」，系统从此天天报偏离 ——
        # 这不是一种口径，是配错了。
        sa.CheckConstraint("concentration_n >= 1", name="concentration_n_positive"),
        sa.CheckConstraint(
            "same_material_cross_aisle_threshold >= 1",
            name="same_material_cross_aisle_threshold_positive",
        ),
        sa.CheckConstraint(
            "same_batch_cross_aisle_threshold >= 1",
            name="same_batch_cross_aisle_threshold_positive",
        ),
        # 漂移告警阈值严格为正：16 §6.4 用它决定「是否以快照重算值校正基线」，
        # 阈值 0 意味着任何微小差异都算漂移 —— 校正会被触发到基线永远追不上现场。
        sa.CheckConstraint("cap_drift_alert_threshold > 0.0", name="cap_drift_alert_threshold_positive"),
    )

    #: 业务版本号（D1），理由同 `WeightConfig.version_no`。
    version_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    #: 生效时间（naive UTC），理由同 `WeightConfig.effective_at`。
    effective_at: Mapped[datetime] = mapped_column(nullable=False)

    #: 近站台预留比例（16 §353~356：默认 40%）。存 0~1 的比例而非百分数：与
    #: 17 §10.6 的 `adoption_rate` 同一处置，避免「40 还是 0.40」在库里流转。
    near_station_reserved_ratio: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.40
    )

    #: 预留超时释放的**当日钟点**（16 §353~356：18:00）。见模块 docstring 第 4 条：
    #: 是本地墙上时间，不是 naive UTC。
    reserved_release_at: Mapped[time] = mapped_column(
        sa.Time, nullable=False, default=time(18, 0)
    )

    #: 加权集中度的 N（16 §353~356：默认 5）。统一验收指标是「80% 拣货量落在 ≤N 巷道」
    #: （CLAUDE.md §十），N 正是这里的这一项 —— 它属配置而不属 KPI 实体，
    #: 故 `KpiSnapshot` 不存 N，卡片 JSON 引用当次生效的配置行（9.4e③）。
    concentration_n: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)

    #: 同物料跨巷道阈值（默认 5，验收基线同值）。
    same_material_cross_aisle_threshold: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=5
    )

    #: 同批跨巷道阈值（默认 3，验收基线同值）。
    same_batch_cross_aisle_threshold: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=3
    )

    #: cap 漂移告警阈值（16 §353~356「建议 > 1%」，故默认 0.01 = 1%）。
    cap_drift_alert_threshold: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.01
    )

    # 注：本表**没有** `changed_by_id`。17 §七 把「变更人」列在权重配置上，
    # 容量配置的字段表没有它（容量的六项来自 16）。不替文档补列 —— 补了之后
    # 「为什么这张表能查变更人、那张不能」就没人答得上来了。


class FieldMappingConfig(BaseEntity):
    """字段映射配置（`17` §七 / `16` 附录 A.1）。

    一行 = 一类文件的源列名 → 本系统字段的映射。**内置**（按单厂编码），
    首期不提供可视化配置（v0.11 决策），故不带版本号（模块 docstring 第 1 条）。
    """

    __tablename__ = "field_mapping_configs"
    __table_args__ = (
        # 一台设备一类文件只有一份映射。带 `warehouse_id` 是隔离列的同一处置：
        # 单厂试点下二者同值，但键里放它就不必在跨厂时改约束（改约束要写迁移）。
        # 若同一类型能有两份映射，导入时取哪份都说不清，且两份会随维护逐渐分叉。
        sa.UniqueConstraint(
            "warehouse_id", "plant_code", "file_type",
            name="uq_field_mapping_configs_warehouse_plant_file_type",
        ),
    )

    #: 工厂编码（17 §七；`16` 附录 A.1 表头的「工厂编码」列）。文本 —— 厂编码含字母，
    #: 不得数值化（CLAUDE.md 第七节）。它是本表业务键的一部分而不是只有 warehouse_id：
    #: 现场文件里带的是**工厂编码**，导入时要按文件内容定位映射，故必须能直接查它。
    plant_code: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    #: 文件类型（17 §九 的 `FileType`）：PO / DO / INV 三类各有模版
    #: （`16` 附录 A.1/A.2/A.3）。
    file_type: Mapped[FileType] = enum_column(FileType, name="file_type", nullable=False)

    #: 源列名 → 本系统字段的映射（17 §七「源列名 → 本系统字段」），每项含
    #: `target` / `required` / `rule` 三个键（`16` 附录 A.1 的「映射到本系统字段 /
    #: 是否必填 / 校验规则」三列）。**NOT NULL**，但允许空字典：空映射意味着
    #: 「这份配置已存在但还没填」，与「没有这份配置」是两件事 —— 导入时前者应当
    #: 报「映射不完整」，后者报「缺映射配置」，两个错指向不同的动作。
    #: **不按列序号硬取字段**（CLAUDE.md 第七节）也正落在这里：映射按列名而非序号。
    mapping_json: Mapped[dict] = mapped_column(sa.JSON, nullable=False)


class PromptTemplate(BaseEntity):
    """问句模板（`17` §七 / `15` §8.2、§8.3）。

    一行 = 一条可点击问句及其映射的冷路径能力。与字段映射相反，**它是可增删的**：
    15 §8.5「新增一条问句即新增一条映射，无需改代码」，故默认启用（新增即生效）。
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        # 模板 ID 在一个仓库内唯一。**用 `warehouse_id` + `template_id` 而不是自增 id
        # 当业务键**：15 §8.3 的前端按模板 ID 发起调用，ID 是接口契约的一部分。
        sa.UniqueConstraint(
            "warehouse_id", "template_id", name="uq_prompt_templates_warehouse_template_id"
        ),
    )

    #: 模板 ID（17 §七），如 `KPI_DIGEST`。ASCII 大写 —— 它出现在前端调用参数里，
    #: 与 `Capability` 的中文**取值**形成对照：ID 是接口标识（不该随文案改），
    #: 能力是给人看的分类（跟着文档走）。
    template_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 问句文本（17 §七）。带 `{}` 的即 15 §8.3 的参数化模板。
    #: 长度 255：最长的样本是「帮我生成茉莉柚茶的收拢方案，代价多大？」（15 §8.2），
    #: 余量留给参数化后的物料名。
    question_text: Mapped[str] = mapped_column(sa.String(255), nullable=False)

    #: 映射的冷路径能力（17 §七）。四类见 `Capability` —— 拼错的能力值会让这条模板
    #: **永远不会被调用**，而它看起来是启用的，故 D3 要求第二层 DB CHECK 看住它。
    capability: Mapped[Capability] = enum_column(
        Capability, name="capability", nullable=False
    )

    #: 参数占位名列表（15 §8.3：参数化语句点击后弹物料选择器）。
    #: 与 `question_text` 里的 `{}` 是同一件事的两种形态：文本是渲染源，
    #: 本列是选择器的取值来源。可空 —— 四条内置问句里只有一条是参数化的。
    params_json: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)

    #: 启用状态（17 §七）。默认 True：15 §8.5 的「新增一条问句即新增一条映射」是
    #: 新增即生效的语义；默认 False 会让新问句悄悄点不到，而没人会记得回来开开关。
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class ConversationContext(BaseEntity):
    """对话台上下文（`17` §八 / §十二）。

    一行 = 一次会话的短期上下文。**不是长期记忆**：本项目明确不做自主 Agent
    （CLAUDE.md 第九节），17 §十二 要求会话级留存（建议 ≤8 小时或关闭清除），
    故 `expires_at` 必填 —— 没有过期时间的会话行会永久留下用户的问句原文。
    """

    __tablename__ = "conversation_contexts"
    __table_args__ = (
        # 会话号是业务键（17 §八「会话 ID」）。唯一范围按仓库 —— 与其余 22 张表同一隔离口径。
        sa.UniqueConstraint(
            "warehouse_id", "session_no", name="uq_conversation_contexts_warehouse_session_no"
        ),
    )

    #: 会话 ID（17 §八）。文本而非自增主键：前端持有它、断线重连要带上它，
    #: 是接口契约的一部分。
    session_no: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: 用户（17 §八）。**NOT NULL 且指向账号**：对话台是可追责的一环 ——
    #: 它承载「待确认的写操作上下文」，一个没有主体的写操作意图既不能审计也不能授权。
    #: 列名用 `account_id` 而非 `user_id`：实体名是 `Account`，与
    #: `JobOrder.confirmed_by_id` / `Ledger.operator_id` 一起都指向 `accounts.id`。
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False)

    #: 当前页面上下文（17 §八），如 `relocate`。可空 —— 从对话台直接进入时没有页面来源。
    page_context: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    #: 最近问句与结果卡片引用（17 §八）。落 JSON：D4 只把 17 §10 的 6 类结构定为 JSON 列，
    #: 本项不在其中，但它的形状是「一个随对话增长的引用列表」，文档未给条数上限与
    #: 单条形状，用一个 JSON 容器装下比替文档定一个表结构安全。
    recent_turns_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 待确认的写操作（确认卡）上下文（17 §八）。见模块 docstring 第 5 条：
    #: 只落上下文，不建确认卡实体。
    pending_write_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)

    #: 本轮对话绑定的作业单（可空）。**外键而不是存单号字符串**：作业单状态会在
    #: 对话进行中被改（确认 → 执行），存字符串会拿到一个过期的单据快照，
    #: 且「这单现在什么状态」要靠再查一次才答得上。可空 —— 对话台多数问句是
    #: 看板/分析类（15 §8.2 的四条问句里只有移库方案那条绑单）。
    job_order_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("job_orders.id"), nullable=True
    )

    #: 过期时间（17 §八「创建·过期时间」、§十二）。**必填**，见类 docstring。
    #: 滑动续期由**重写本列**表达（模块 docstring 第 6 条），不另立 `updated_at`。
    #: 存储口径是 naive UTC（与 `created_at` 一致）。
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
