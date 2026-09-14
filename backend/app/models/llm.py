"""冷路径链 3 实体（`10` 号 / `29` 号定义）。

事实来源：10-AI 辅助能力（冷路径）设计 §七（成本硬上限 / 数据脱敏）
          15-05-对话台+接口权限+测试验收 §4.1（ConversationLog / AiSuggestion）
          29 号（AiCostQuota）
          openspec/changes/ai-assist/design.md D7（3 冷路径实体，非台账）
          spec `data-model`「实体清单与数据链分组」

  AiSuggestion(建议卡片，只读) / ConversationLog(对话日志) / AiCostQuota(配额记账)

关键口径：
  - 三者均 **warehouse_id 隔离、不进 `Ledger` 台账链**（spec「冷路径实体非台账」）。
    `AiSuggestion` 是「建议」不是「决定」，只读；`AiCostQuota` 仅配额记账，可审计。
  - `AiCostQuota` 是**同步记账**的落点：每次冷路径调用后 `tokens_consumed` 累加、
    `updated_at` 刷新 —— 落库而不落内存，否则重启即失、月预算无法审计（D7）。
  - `AiSuggestion.context_json` 存「建议背后的规则侧结构化数据」（③ 的拟采纳权重、
    ④ 的多方案、① 的 KPI 数值）—— 不是 LLM 产物，是「规则算的数」。`weight/apply`
    正是读它才知道该写哪组权重（红线 3：LLM 产出不经规则校验不进台账，采纳的是
    **规则算的数**，不是 LLM 的叙事文本）。
  - `ConversationLog.hit_cold_path` 区分「走了外部 LLM」与「L0 确定性直答」——
    审计「数据出域 = 0」时按它筛选，不用去翻叙事文本。

本模块是「冷路径链」—— 与 `configuration.py` 的 `ConversationContext`（对话台会话）
不同：`ConversationContext` 是会话级（复用，见 D7），这里三个是**每轮建议 / 每轮日志 /
每周期配额**的落点，各自生命周期不同，不并进配置链。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseEntity, enum_column, utcnow
from app.models.configuration import Capability


class AiSuggestionStatus(str, Enum):
    """建议采纳状态。

    **实体局部值域**（不进 `app/core/enums.py`，D2 同一口径）：只在 `AiSuggestion`
    内有意义。`PROPOSED` = 影子模式（**不生效**，D12「建议默认影子模式」）——
    建议被展示、被复核，但不落任何写路径；`ADOPTED` = 人经 `ai.weight.update` 采纳，
    由规则校验后写入 `WeightConfig`；`DISMISSED` = 驳回（留档不生效）。
    """

    PROPOSED = "PROPOSED"
    ADOPTED = "ADOPTED"
    DISMISSED = "DISMISSED"


class AiSuggestion(BaseEntity):
    """建议卡片（`15-05` §4.1 / `10` §七）。**只读** —— 建议不直接改任何状态。

    一行 = 一条冷路径能力产出的一次建议。`suggestion_text` 是 LLM 叙事（可降级缺席的
    部分由响应层处理，此处存已产出的文本），`context_json` 是它背后的**规则侧数据**。
    """

    __tablename__ = "ai_suggestions"

    #: 映射的冷路径能力（四类，见 `Capability`）。与 `PromptTemplate.capability`
    #: 同一枚举 —— 建议总是某个能力的产物，拼错能力值会让建议无法被消费。
    capability_kind: Mapped[Capability] = enum_column(
        Capability, name="capability", nullable=False
    )

    #: 建议文本（LLM 叙事）。长度不定，用 `Text`：KPI 报告 / 归因 / 多方案权衡
    #: 都是成段话，`String(n)` 的 n 拍脑袋。
    suggestion_text: Mapped[str] = mapped_column(sa.Text, nullable=False)

    #: 采纳状态。默认 `PROPOSED`（影子模式，不生效）—— 见 `AiSuggestionStatus`。
    status: Mapped[AiSuggestionStatus] = enum_column(
        AiSuggestionStatus,
        name="ai_suggestion_status",
        nullable=False,
        default=AiSuggestionStatus.PROPOSED,
    )

    #: 规则侧结构化数据（可空）。③ 存拟采纳权重、④ 存多方案、① 存 KPI 数值 ——
    #: 它是「采纳落地」要校验、要写入的来源，见模块 docstring。
    context_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)


class ConversationLog(BaseEntity):
    """对话日志（`15-05` §4.1）。一行 = 对话台一轮问句。

    记「问句原文 / 脱敏后文本 / LLM 产出 / 是否命中冷路径」四项 + 意图与槽位 ——
    后两者是审计「写意图路由到确认卡、未直写台账」的凭据。`question_raw` 可能含
    `order_no` / 操作员姓名（未脱敏），故**只在库内**、永不出站（出站只走
    `question_redacted`，`llm/redact.py`）。
    """

    __tablename__ = "conversation_logs"

    #: 发问人。NOT NULL 且指向账号：与 `ConversationContext.account_id` 同一处置 ——
    #: 对话台是可追责的一环，写意图的审计链要有主体。
    account_id: Mapped[int] = mapped_column(sa.ForeignKey("accounts.id"), nullable=False)

    #: 识别出的意图（D9 的 `{KPI_INTERPRET, DEVIATION_ATTRIBUTE, WEIGHT_TUNE,
    #: RELOCATE_PROPOSE}`）。存**文本**而非枚举 + CHECK：意图词汇是 `app/llm` 的
    #: 路由契约（将来 L2 模型换版可能扩词表），不是领域不变量 —— 日志只需留痕，
    #: 不需要 DB 层把取值钉死。
    intent: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    #: 问句原文（未脱敏）。**永不出站** —— 出站只用脱敏后的 `question_redacted`。
    question_raw: Mapped[str] = mapped_column(sa.Text, nullable=False)

    #: 脱敏后文本（可空）。L0 确定性问句不出站时无脱敏产物，此处为 NULL；
    #: 只有命中冷路径、组装出站 payload 的那一轮才有值。
    question_redacted: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: LLM 产出（可空）。降级（provider 未配置 / 预算耗尽 / 超时 / 不可用）时无产出。
    llm_output: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: 是否命中冷路径（是否发起了外部 LLM 调用）。审计「数据出域 = 0」按它筛选。
    hit_cold_path: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    #: 意图槽位（可空）：①`{period}` ②`{material_code}` ③`{}` ④`{material_code}`。
    slots_json: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)


class AiCostQuota(BaseEntity):
    """成本配额记账（`29` 号 / `10` §七 成本护栏第 ③ 道）。

    一行 = 一个仓库一个预算周期（月）的累计消费。**同步记账**：每次冷路径调用后
    `tokens_consumed` 累加、`updated_at` 刷新；入口在「累加前」判是否已达
    `llm_monthly_budget`，达则熔断（`budget_exhausted`），不发起调用（D3）。
    """

    __tablename__ = "ai_cost_quotas"
    __table_args__ = (
        # 一个仓库一个周期只有一行 —— 否则「本月累计消费」要跨多行求和，而
        # 「取哪几行」这个判断一旦漂移，熔断阈值就和记账对不上。
        sa.UniqueConstraint(
            "warehouse_id", "period", name="uq_ai_cost_quotas_warehouse_period"
        ),
        # 消费量非负：负消费会让「是否已达预算」的判定方向反掉。
        sa.CheckConstraint("tokens_consumed >= 0", name="tokens_consumed_non_negative"),
    )

    #: 预算周期（月），`"YYYY-MM"`。月度预算按它归零（新周期 = 新行）。
    period: Mapped[str] = mapped_column(sa.String(7), nullable=False)

    #: 本周期累计消费 token 数。
    tokens_consumed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    #: 最近一次记账时间。与继承的 `created_at`（首行创建时间）分开：D7 点名要
    #: `updated_at` —— 配额是「原地累加」的，审计要问「最后是什么时候加的」。
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
