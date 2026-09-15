"""L2 对话台 NLU（自由文本 → `NluResult`）契约测试（tasks.md 4.2 的验证）。

事实来源：openspec/changes/ai-assist/design.md D9（L2 走外部 LLM NLU，脱敏）
          spec `ai-assist`「对话台 L0/L2 意图识别」

钉住三件事：① 未接 provider → `provider_unconfigured`（原样带出，不猜意图）；
② LLM 回了合法 `{"intent","slots"}` JSON → 解析成 `NluResult.ok`；③ LLM 回了叙事 / 缺
`intent` 键 / 非法 JSON → `intent_unrecognized`（fail-closed，端点据此不路由、不直出）。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.llm import DegradedReason, client
from app.llm.client import Completion
from app.llm.nlu import NluResult, recognize_intent

pytestmark = pytest.mark.logic


def test_unconfigured_provider_returns_provider_unconfigured() -> None:
    """`llm_provider=""` → `provider_unconfigured`（不是 `intent_unrecognized`，各归各）。"""
    result = recognize_intent("帮我分析本周集中度下滑原因", settings=Settings(llm_provider=""))

    assert result.ok is False
    assert result.intent is None
    assert result.slots == {}
    assert result.degraded_reason == "provider_unconfigured"


def test_valid_intent_json_parses_to_nlu_result(monkeypatch) -> None:
    """LLM 回 `{"intent":"KPI_INTERPRET","slots":{"period":"2026-09"}}` → 解析成功 + 出站指令正确。"""
    captured: dict = {}

    def fake_complete(prompt, *, settings=None, timeout_s=None, system=None):
        captured.update(prompt=prompt, system=system)
        return Completion(text='{"intent": "KPI_INTERPRET", "slots": {"period": "2026-09"}}')

    monkeypatch.setattr(client, "complete", fake_complete)

    result = recognize_intent("帮我分析本周集中度下滑原因")

    assert result.ok is True
    assert result.intent == "KPI_INTERPRET"
    assert result.slots == {"period": "2026-09"}
    assert result.degraded_reason is None
    # 出站 payload 只含脱敏后的 question，不含任何字段名（数据出域护栏）。
    assert json.loads(captured["prompt"]) == {"question": "帮我分析本周集中度下滑原因"}
    # 出站 system 指令是「意图分类」而非叙事（与 client._SYSTEM_PROMPT 分工）。
    assert "intent" in captured["system"]


def test_narration_text_returns_intent_unrecognized(monkeypatch) -> None:
    """LLM 回了叙事（非 JSON）→ `intent_unrecognized`（这是本次 bug 的根因：叙事被当 JSON 解析）。"""

    def fake_complete(prompt, *, settings=None, timeout_s=None, system=None):
        return Completion(text="请把本周的 JSON 数据发给我，我才能分析集中度下滑原因")

    monkeypatch.setattr(client, "complete", fake_complete)

    result = recognize_intent("帮我分析本周集中度下滑原因")

    assert result.ok is False
    assert result.intent is None
    assert result.degraded_reason == "intent_unrecognized"


def test_missing_intent_key_returns_intent_unrecognized(monkeypatch) -> None:
    """LLM 回了 JSON 但缺 `intent` 键 → `intent_unrecognized`（fail-closed，不猜）。"""

    def fake_complete(prompt, *, settings=None, timeout_s=None, system=None):
        return Completion(text='{"slots": {}}')

    monkeypatch.setattr(client, "complete", fake_complete)

    result = recognize_intent("随便一句话")

    assert result.ok is False
    assert result.degraded_reason == "intent_unrecognized"


def test_llm_timeout_preserves_original_reason(monkeypatch) -> None:
    """LLM 侧降级（超时）→ 原样带出 `llm_timeout`，不吞成 `intent_unrecognized`。"""

    def fake_complete(prompt, *, settings=None, timeout_s=None, system=None):
        return Completion(degraded_reason=DegradedReason.LLM_TIMEOUT)

    monkeypatch.setattr(client, "complete", fake_complete)

    result = recognize_intent("帮我分析本周集中度下滑原因")

    assert result.ok is False
    assert result.degraded_reason == "llm_timeout"


def test_nlu_result_ok_invariant() -> None:
    """`ok` = 有意图且无降级原因；其余组合一律 falsy。"""
    assert NluResult(intent="KPI_INTERPRET", slots={}, degraded_reason=None).ok is True
    assert NluResult(intent=None, slots={}, degraded_reason="provider_unconfigured").ok is False
    assert NluResult(intent="KPI_INTERPRET", slots={}, degraded_reason="intent_unrecognized").ok is False
