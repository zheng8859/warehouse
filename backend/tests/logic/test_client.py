"""外部 LLM 客户端（provider 抽象 / 超时 / mock）契约测试（tasks.md 2.2 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §七（超时默认 2.0s）
          openspec/changes/ai-assist/design.md D3（`llm_provider` 空 = 不调用）
          spec `ai-assist`「LLM 侧失败降级不报错」

三件事被钉住：① `llm_provider` 空 = 未配置 = 不发起任何调用（`provider_unconfigured`）；
② 阻塞调用超 `llm_request_timeout_s` 返回 `llm_timeout`；③ mock 客户端确定性回显。
「不发起调用」用注入 backend 的方式证明 —— provider 空时 backend 根本不会被求值。
"""
from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.llm import DegradedReason
from app.llm.client import Completion, complete

pytestmark = pytest.mark.logic


def test_empty_provider_returns_provider_unconfigured_without_calling() -> None:
    """`llm_provider=""` → `provider_unconfigured`，且 backend 不被求值（= 零外部调用）。"""
    called: list[str] = []

    def backend(prompt: str) -> str:
        called.append(prompt)
        return "不该被调用"

    result = complete("上周集中度怎么样", settings=Settings(llm_provider=""), backend=backend)

    assert result.text is None
    assert result.degraded_reason is DegradedReason.PROVIDER_UNCONFIGURED
    assert result.ok is False
    assert called == []


def test_mock_provider_returns_deterministic_echo() -> None:
    """mock 客户端确定性回显 —— 同输入同输出，不外发。"""
    settings = Settings(llm_provider="mock")

    first = complete("上周集中度怎么样", settings=settings)
    second = complete("上周集中度怎么样", settings=settings)

    assert first.ok is True
    assert first.text == "[mock] 上周集中度怎么样"
    assert first.degraded_reason is None
    assert second.text == first.text


def test_unknown_provider_returns_llm_unavailable() -> None:
    """未接线的真实 provider（Phase A 只有 mock）→ `llm_unavailable`，不崩溃。"""
    result = complete("x", settings=Settings(llm_provider="openai"))

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_UNAVAILABLE
    assert result.ok is False


def test_slow_backend_times_out_to_llm_timeout() -> None:
    """阻塞调用超过 `llm_request_timeout_s` → `llm_timeout`（不 5xx、不中断）。"""
    def slow(prompt: str) -> str:
        time.sleep(0.3)
        return "晚到的回复"

    result = complete("x", settings=Settings(llm_provider="mock"), timeout_s=0.01, backend=slow)

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_TIMEOUT
    assert result.ok is False


def test_completion_ok_is_truthy_only_on_real_success() -> None:
    """`ok` 的判据 = 有文本且无降级原因；其余组合一律 falsy。"""
    assert Completion(text="x", degraded_reason=None).ok is True
    assert Completion(text=None, degraded_reason=DegradedReason.LLM_TIMEOUT).ok is False
    assert Completion(text=None, degraded_reason=None).ok is False
