"""外部 LLM 客户端（provider 抽象 / 超时 / mock）契约测试（tasks.md 2.2 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §七（超时默认 10.0s，接真模型后上调）
          openspec/changes/ai-assist/design.md D3（`llm_provider` 空 = 不调用）
          spec `ai-assist`「LLM 侧失败降级不报错」

三件事被钉住：① `llm_provider` 空 = 未配置 = 不发起任何调用（`provider_unconfigured`）；
② 阻塞调用超 `llm_request_timeout_s` 返回 `llm_timeout`；③ mock 客户端确定性回显。
「不发起调用」用注入 backend 的方式证明 —— provider 空时 backend 根本不会被求值。
"""
from __future__ import annotations

import time

import httpx
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
    """未接线的 provider（如 gemini）→ `llm_unavailable`，不崩溃、不外发。"""
    result = complete("x", settings=Settings(llm_provider="gemini"))

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_UNAVAILABLE
    assert result.ok is False


def test_openai_provider_without_key_returns_llm_unavailable() -> None:
    """provider="openai" 但没配 key → `llm_unavailable`（不带空 key 出站）。"""
    result = complete("x", settings=Settings(llm_provider="openai"))

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_UNAVAILABLE
    assert result.ok is False


def test_openai_compatible_backend_posts_and_parses(monkeypatch) -> None:
    """provider="openai" + key → 真实 OpenAI 兼容后端：出站 payload / Bearer / 解析正确。"""
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "解读文本"}}]}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _FakeResp()

    monkeypatch.setattr("httpx.post", fake_post)

    settings = Settings(
        llm_provider="openai",
        llm_api_key="sk-test",
        llm_base_url="https://api.example.com/v1",
        llm_model="test-model",
    )
    result = complete("一段数据", settings=settings)

    assert result.ok is True
    assert result.text == "解读文本"
    assert result.degraded_reason is None
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"][0]["role"] == "system"
    assert captured["json"]["messages"][0]["content"]  # system 指令非空
    assert captured["json"]["messages"][1] == {"role": "user", "content": "一段数据"}


def test_deepseek_provider_is_recognized_case_insensitively(monkeypatch) -> None:
    """provider="DeepSeek"（大写）也能解析到 OpenAI 兼容后端（匹配大小写不敏感）。"""
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _FakeResp()

    monkeypatch.setattr("httpx.post", fake_post)

    settings = Settings(
        llm_provider="DeepSeek",
        llm_api_key="sk-test",
        llm_base_url="https://api.deepseek.com/v1",
        llm_model="deepseek-chat",
    )
    result = complete("x", settings=settings)

    assert result.ok is True
    assert result.text == "ok"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "deepseek-chat"


def test_openai_backend_network_error_degrades_to_llm_unavailable(monkeypatch) -> None:
    """上游不可达 → 降级 `llm_unavailable`，不把 LLM 故障报成 5xx。"""

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", fake_post)
    result = complete("x", settings=Settings(llm_provider="openai", llm_api_key="sk-test"))

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_UNAVAILABLE
    assert result.ok is False


def test_openai_backend_httpx_timeout_degrades_to_llm_timeout(monkeypatch) -> None:
    """上游超时（httpx 内部超时）→ 降级 `llm_timeout`（与外层 future 超时同语义）。"""

    def fake_post(*args, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr("httpx.post", fake_post)
    result = complete("x", settings=Settings(llm_provider="openai", llm_api_key="sk-test"))

    assert result.text is None
    assert result.degraded_reason is DegradedReason.LLM_TIMEOUT
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
