"""对话台 L0/L2 意图路由表：4 类意图 → 能力 / 槽位 / write_intent。D9。

事实来源：openspec/changes/ai-assist/design.md D9（L2 意图 = 4 类 + slots + write_intent）
          spec `ai-assist`「对话台 L0/L2 意图识别」
          doc 10 §八（四类能力）、15-05 §3.3（L2 复杂问句映射）

L2 识别的意图词汇是 `{KPI_INTERPRET, DEVIATION_ATTRIBUTE, WEIGHT_TUNE, RELOCATE_PROPOSE}`
（D9 定名；既非 `Capability` 的中文取值，也非 `PromptTemplate.template_id`）。本模块把
「意图 → 能力 / 槽位 / 是否写意图」钉成一张**确定性表**：LLM（L2 NLU）只产出「意图 +
槽位」，**是否写意图、需要哪些槽位**由本表决定 —— 写意图的判定是安全边界（写意图路由
二次确认卡、不直接执行），不能交给 LLM 现判（红线 2「LLM 不直接执行写操作」）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.models.configuration import Capability

#: L2 意图词汇（D9）。顺序 = doc 10 §八 四类能力的展示序。
INTENT_KINDS: tuple[str, ...] = (
    "KPI_INTERPRET",
    "DEVIATION_ATTRIBUTE",
    "WEIGHT_TUNE",
    "RELOCATE_PROPOSE",
)

#: 意图 → 冷路径能力（四类，见 `Capability`）。
_INTENT_CAPABILITY: Mapping[str, Capability] = {
    "KPI_INTERPRET": Capability.KPI_DIGEST,
    "DEVIATION_ATTRIBUTE": Capability.DEVIATION_ATTRIBUTION,
    "WEIGHT_TUNE": Capability.WEIGHT_TUNING,
    "RELOCATE_PROPOSE": Capability.RELOCATE_PLAN,
}

#: 意图 → 槽位键（D9）：①`{period}` ②`{material_code}` ③`{}` ④`{material_code}`。
_INTENT_SLOTS: Mapping[str, frozenset[str]] = {
    "KPI_INTERPRET": frozenset({"period"}),
    "DEVIATION_ATTRIBUTE": frozenset({"material_code"}),
    "WEIGHT_TUNE": frozenset(),
    "RELOCATE_PROPOSE": frozenset({"material_code"}),
}

#: 写意图（③④ = true）：路由二次确认卡，不直接执行。
_WRITE_INTENTS: frozenset[str] = frozenset({"WEIGHT_TUNE", "RELOCATE_PROPOSE"})


@dataclass(frozen=True)
class RecognizedIntent:
    """一次意图识别的确定性结果：意图名 + 映射能力 + 槽位 + 是否写意图。"""

    intent: str
    capability: Capability
    slots: Mapping[str, str]
    write_intent: bool


def is_write_intent(intent: str) -> bool:
    """③④ 是写意图。未知意图**不算**写意图 —— fail-closed（见 `route_intent`）。"""
    return intent in _WRITE_INTENTS


def route_intent(intent: str, slots: Mapping[str, str] | None = None) -> RecognizedIntent:
    """把 L2 识别出的「意图 + 槽位」路由成确定性结果（纯函数）。

    **fail-closed**：

    - 未知意图 → `ValueError`：意图词汇是路由契约，拼错意味着识别端输出了不在表里的
      词，该显式报错，而不是静默落成某个读意图直出 —— 静默会把一个写操作意图当成
      只读直出，正是红线 2 要拦的。
    - 槽位键越界 → `ValueError`：「多出的槽位」说明识别端与路由表的契约分叉，报错比
      忽略更安全（多一个 `material_code` 对读意图无害，但契约分叉迟早伤害写意图）。

    **不强制「必需槽位在场」**：那是各能力自己的入参要求（②④ 要 `material_code`、① 要
    `period`），由对话台端点按能力判 —— 本表只管「哪些槽位合法」，不管「哪个必需」。
    """
    if intent not in _INTENT_CAPABILITY:
        raise ValueError(f"未知意图 {intent!r}（合法值 {list(INTENT_KINDS)}）")

    slots = dict(slots or {})
    allowed = _INTENT_SLOTS[intent]
    extra = set(slots) - allowed
    if extra:
        raise ValueError(
            f"意图 {intent} 不接受槽位 {sorted(extra)}（合法槽位 {sorted(allowed)}）"
        )

    return RecognizedIntent(
        intent=intent,
        capability=_INTENT_CAPABILITY[intent],
        slots={key: slots[key] for key in allowed if key in slots},
        write_intent=is_write_intent(intent),
    )
