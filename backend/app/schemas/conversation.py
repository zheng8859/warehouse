"""对话台统一入口 `POST /api/conversation/message` 的 DTO。D9 / D13。

事实来源：openspec/changes/ai-assist/design.md D9（L0/L2 意图 + write_intent）、D13（端点表）
          spec `ai-assist`「对话台 L0/L2 意图识别」

L0 = 结构化入口（`intent` + `slots`，确定性路由）；L2 = 自由文本（`question`，脱敏后走
外部 LLM NLU）。两者**互斥**：同一轮要么给结构化意图、要么给自由文本，同给或都不给都
是报文错误（422）—— 隐式「两选一」会让「intent 拼错却给了 question」被静默吞成 L2。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from app.schemas.llm import with_ai_notice


class ConversationMessageRequest(BaseModel):
    """对话台一轮问句。`intent`（L0）与 `question`（L2）必须且只能给一个。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    #: L0 结构化：显式意图（D9 的四类词汇），槽位合法性由 `route_intent` 校验。
    intent: str | None = None
    slots: dict[str, str] | None = None
    #: L2 自由文本：脱敏后走外部 LLM NLU。
    question: str | None = None

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "ConversationMessageRequest":
        if (self.intent is not None) == (self.question is not None):
            raise ValueError("intent（L0 结构化）与 question（L2 自由文本）必须且只能给一个")
        if self.intent is None and self.slots is not None:
            raise ValueError("slots 只能随 L0 结构化 intent 一起给出")
        return self


class ConversationMessageResponse(BaseModel):
    """对话台一轮的响应：意图 + `write_intent` + 双产物（`rule` / `ai` / `ai_generated` /
    `degraded_reason`）。

    `intent` 可空：仅 L2 NLU 识别失败时（降级为「无法识别」）为 `None`；识别成功的
    每一轮 `intent` 必非空。`write_intent=true` 只回建议、不写台账，前端据此弹二次确认卡。
    """

    intent: str | None
    write_intent: bool
    rule: dict[str, Any]
    ai: str | None = None
    ai_generated: bool
    degraded_reason: str | None = None

    @field_serializer("ai")
    def _inject_ai_notice(self, ai: str | None) -> str | None:
        """同 `DualProductResponse`：`ai` 序列化时强制注入 AI Notice（spec「AI 建议标注」）。"""
        return with_ai_notice(ai)
