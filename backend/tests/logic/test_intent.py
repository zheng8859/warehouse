"""对话台 L0/L2 意图路由表测试（tasks.md 4.1 的验证）。

事实来源：openspec/changes/ai-assist/design.md D9
          spec `ai-assist`「对话台 L0/L2 意图识别」

核心不变量：意图 → 能力 / 槽位 / write_intent 是**确定性**映射；③④ 是写意图
（`write_intent=true`，路由二次确认卡），①② 是读意图（`write_intent=false`，直出结果）；
未知意图与越界槽位 fail-closed（`ValueError`），不静默落成读意图。
"""
from __future__ import annotations

import pytest

from app.llm.intent import INTENT_KINDS, is_write_intent, route_intent
from app.models.configuration import Capability

pytestmark = pytest.mark.logic


def test_intent_vocabulary_is_four_kinds() -> None:
    """意图词汇恰为 D9 的四类，顺序即 doc 10 §八 的展示序。"""
    assert INTENT_KINDS == (
        "KPI_INTERPRET",
        "DEVIATION_ATTRIBUTE",
        "WEIGHT_TUNE",
        "RELOCATE_PROPOSE",
    )


def test_intent_maps_to_capability() -> None:
    """四类意图 → 四类冷路径能力（一对一，确定性）。"""
    expected = {
        "KPI_INTERPRET": Capability.KPI_DIGEST,
        "DEVIATION_ATTRIBUTE": Capability.DEVIATION_ATTRIBUTION,
        "WEIGHT_TUNE": Capability.WEIGHT_TUNING,
        "RELOCATE_PROPOSE": Capability.RELOCATE_PLAN,
    }
    for intent, capability in expected.items():
        assert route_intent(intent).capability is capability


def test_read_intents_are_not_write() -> None:
    """①② 是读意图：`write_intent=false`，直出结果卡片。"""
    assert route_intent("KPI_INTERPRET").write_intent is False
    assert route_intent("DEVIATION_ATTRIBUTE").write_intent is False
    assert is_write_intent("KPI_INTERPRET") is False
    assert is_write_intent("DEVIATION_ATTRIBUTE") is False


def test_write_intents_route_to_confirmation() -> None:
    """③④ 是写意图：`write_intent=true`，路由二次确认卡、不直接写台账。"""
    assert route_intent("WEIGHT_TUNE").write_intent is True
    assert route_intent("RELOCATE_PROPOSE").write_intent is True
    assert is_write_intent("WEIGHT_TUNE") is True
    assert is_write_intent("RELOCATE_PROPOSE") is True


def test_slots_per_intent() -> None:
    """槽位键按 D9：①{period} ②{material_code} ③{} ④{material_code}。"""
    kpi = route_intent("KPI_INTERPRET", {"period": "2026-09"})
    assert kpi.slots == {"period": "2026-09"}

    deviation = route_intent("DEVIATION_ATTRIBUTE", {"material_code": "3001234"})
    assert deviation.slots == {"material_code": "3001234"}

    # ③ 无槽位：即便传入空也不产出槽位。
    assert route_intent("WEIGHT_TUNE").slots == {}

    relocate = route_intent("RELOCATE_PROPOSE", {"material_code": "3001234"})
    assert relocate.slots == {"material_code": "3001234"}


def test_unknown_intent_fails_closed() -> None:
    """未知意图报错，且 `is_write_intent` 不把它当写意图（fail-closed）。"""
    with pytest.raises(ValueError):
        route_intent("NOT_AN_INTENT")
    assert is_write_intent("NOT_AN_INTENT") is False


def test_extra_slot_fails_closed() -> None:
    """越界槽位报错 —— 契约分叉比忽略更安全。"""
    with pytest.raises(ValueError):
        route_intent("WEIGHT_TUNE", {"material_code": "3001234"})
