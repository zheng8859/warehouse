"""L2 对话台 NLU：脱敏后的自由文本 → `{intent, slots}`。D9。

事实来源：openspec/changes/ai-assist/design.md D9（L2 意图识别走外部 LLM，脱敏）
          spec `ai-assist`「对话台 L0/L2 意图识别」
          doc 10 §五（意图识别走外部 LLM，按 12.5 脱敏）

L2 与 L0 的分工：L0 走 `intent.route_intent`（确定性、不出站）；L2 把自由文本脱敏后
交给外部 LLM NLU，拿回「意图 + 槽位」两个词 —— **`write_intent` 与槽位合法性不由
NLU 决定**（见 `intent.py` 模块 docstring：写意图判定是安全边界，不能交给 LLM 现判）。

Phase A：`llm_provider=""` 时 NLU 无模型可调，`recognize_intent` 返回 `None`，端点据此
降级为「无法识别」，**不猜测意图**（fail-closed：把一个写意图猜成读意图、直出结果，
正是红线 2「LLM 不直接执行写操作」要拦的）。
"""
from __future__ import annotations

import json
import re

from app.core.config import Settings
from app.llm import client

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


def recognize_intent(
    question: str,
    *,
    settings: Settings | None = None,
    timeout_s: float | None = None,
) -> tuple[str, dict[str, str]] | None:
    """脱敏后的自由文本 → `(intent, slots)`；LLM 不可用或解析失败 → `None`。

    出站 payload 只含 `{"question": "<脱敏后文本>"}`，不含任何字段名。解析回的形状是
    `{"intent": "...", "slots": {...}}`；降级（provider 未配置 / 不可用 / 超时）、非法
    JSON、缺 `intent` 键、类型不对 —— 一律 `None`，端点据此不路由、不直出（fail-closed）。
    """
    settings = settings if settings is not None else Settings()
    prompt = json.dumps({"question": redact_text(question)}, ensure_ascii=False)
    completion = client.complete(prompt, settings=settings, timeout_s=timeout_s)
    if not completion.ok:
        return None
    try:
        data = json.loads(completion.text)  # type: ignore[arg-type]
        intent = data["intent"]
        slots = data.get("slots", {})
        if not isinstance(intent, str) or not isinstance(slots, dict):
            return None
        return intent, slots
    except (ValueError, KeyError, TypeError):
        return None
