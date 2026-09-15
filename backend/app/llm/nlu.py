"""L2 对话台 NLU：脱敏后的自由文本 → `{intent, slots}`。D9。

事实来源：openspec/changes/ai-assist/design.md D9（L2 意图识别走外部 LLM，脱敏）
          spec `ai-assist`「对话台 L0/L2 意图识别」
          doc 10 §五（意图识别走外部 LLM，按 12.5 脱敏）

L2 与 L0 的分工：L0 走 `intent.route_intent`（确定性、不出站）；L2 把自由文本脱敏后
交给外部 LLM NLU，拿回「意图 + 槽位」两个词 —— **`write_intent` 与槽位合法性不由
NLU 决定**（见 `intent.py` 模块 docstring：写意图判定是安全边界，不能交给 LLM 现判）。

`llm_provider=""` 时 NLU 无模型可调，`recognize_intent` 返回 `intent_unrecognized` 降级；
接真实 provider 后，用 `_NLU_SYSTEM_PROMPT` 让外部 LLM 只回 `{"intent": ..., "slots": ...}`
JSON，解析失败同样 `intent_unrecognized`。端点据此降级为「无法识别」，**不猜测意图**
（fail-closed：把一个写意图猜成读意图、直出结果，正是红线 2「LLM 不直接执行写操作」要拦的）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.config import Settings
from app.llm import DegradedReason, client

#: 自由文本里「禁出字段名:值」片段的确定性剥除（v1 启发式）。
#:
#: 覆盖 `order_no` / `operator_name` / `operator_note` / `customer_name` / `price` /
#: `supplier` / `recipe` / `true_capacity` 的 `key:value` / `key=value` / `key：value`
#: 形态。结构化 JSON 的正向白名单见 `redact.redact`；自由文本的去标识化（自然语言里
#: 散落的裸订单号 / 姓名）无可靠规则，留待内容安全审核（M14）时一并定义 —— v1 只拦
#: 「显式 key:value 片段」这一种。
_FORBIDDEN_FRAGMENT = re.compile(
    r"(?i)\b("
    r"order_no|operator_name|operator_note|customer_name|price|supplier|recipe|true_capacity"
    r")\s*[:：=]\s*[^\s,，。;；()（）]+"
)


def redact_text(question: str) -> str:
    """自由文本脱敏（v1 启发式）：剥除禁出字段的 `key:value` 片段，其余原样。

    与 `redact.redact`（JSON 正向白名单）分工：那边管「出站 JSON 只含白名单字段」，
    这边管「出站自由文本不夹带禁出字段值」。两者都是「调用外部 LLM 之前」的闸门。
    """
    return _FORBIDDEN_FRAGMENT.sub("[已脱敏]", question)


#: L2 NLU 的出站 system 指令：把自由文本归类为 4 种意图之一，只出 JSON、不编造槽位。
#: 与 `client._SYSTEM_PROMPT`（叙事）分工 —— 那边解读 JSON 事实，这边分类意图。
_NLU_SYSTEM_PROMPT = (
    "你是成品库位智能推荐系统的对话意图识别器。用户会用一句中文描述仓储需求，"
    "你要把它归类为下面 4 种意图之一，并提取槽位。\n"
    "意图（intent 取值）：\n"
    "- KPI_INTERPRET：分析整体集中度 / KPI 走势。槽位 period（形如 YYYY-MM，可选）。\n"
    "- DEVIATION_ATTRIBUTE：分析某个物料偏离 / 散批的原因。槽位 material_code（必填）。\n"
    "- WEIGHT_TUNE：请求调整推荐权重。无槽位。\n"
    "- RELOCATE_PROPOSE：为某个物料生成移库 / 收拢方案。槽位 material_code（必填）。\n"
    "槽位只能从用户原话里提取，不得编造；原话里没有就省略（slots 用空对象 {}）。\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块，形如：\n"
    '{"intent": "KPI_INTERPRET", "slots": {"period": "2026-09"}}'
)


@dataclass(frozen=True)
class NluResult:
    """L2 NLU 的一次结果。成功：`intent` 非空、`degraded_reason is None`；失败反之。"""

    intent: str | None
    slots: dict[str, str]
    degraded_reason: str | None

    @property
    def ok(self) -> bool:
        return self.intent is not None and self.degraded_reason is None


def recognize_intent(
    question: str,
    *,
    settings: Settings | None = None,
    timeout_s: float | None = None,
) -> NluResult:
    """脱敏后的自由文本 → `NluResult`（成功含 `intent`+`slots`；失败含 `degraded_reason`）。

    出站 payload 只含 `{"question": "<脱敏后文本>"}`，不含任何字段名，且用
    `_NLU_SYSTEM_PROMPT` 让 LLM 只回 `{"intent": "...", "slots": {...}}`。失败分两类：
    LLM 侧降级（provider 未配置 / 超时 / 不可用）原样带出原因；LLM 回了但非法 JSON /
    缺 `intent` 键 / 类型不对 → `intent_unrecognized`。两者都 `ok=False`，端点据此不路由、
    不直出（fail-closed）。
    """
    settings = settings if settings is not None else Settings()
    prompt = json.dumps({"question": redact_text(question)}, ensure_ascii=False)
    completion = client.complete(
        prompt, settings=settings, timeout_s=timeout_s, system=_NLU_SYSTEM_PROMPT
    )
    if not completion.ok:
        reason = (
            completion.degraded_reason.value
            if completion.degraded_reason is not None
            else DegradedReason.LLM_UNAVAILABLE.value
        )
        return NluResult(intent=None, slots={}, degraded_reason=reason)
    try:
        data = json.loads(completion.text)  # type: ignore[arg-type]
        intent = data["intent"]
        slots = data.get("slots", {})
        if not isinstance(intent, str) or not isinstance(slots, dict):
            return NluResult(
                intent=None, slots={},
                degraded_reason=DegradedReason.INTENT_UNRECOGNIZED.value,
            )
        return NluResult(intent=intent, slots=slots, degraded_reason=None)
    except (ValueError, KeyError, TypeError):
        return NluResult(
            intent=None, slots={},
            degraded_reason=DegradedReason.INTENT_UNRECOGNIZED.value,
        )
