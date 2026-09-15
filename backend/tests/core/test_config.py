"""运行配置的默认值与环境变量覆盖（tasks.md 1.1 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §七（成本护栏三门槛）
          openspec/changes/ai-assist/design.md D3（成本护栏配置默认值）
          spec `ai-assist`「成本护栏与熔断」

冷路径 4 个新配置项的默认值 = D3 的表值；`WMS_` 前缀覆盖是
`SettingsConfigDict(env_prefix="WMS_")` 的既有机制。这里逐项钉住默认值 + 覆盖值，
防「改了默认却忘了同步 D3」或「env_prefix 拼错导致生产覆盖不生效」。
"""
from __future__ import annotations

from app.core.config import Settings


def test_cold_path_cost_guardrail_defaults() -> None:
    """D3 的四项默认值 + 冷路径总开关默认打开（09 原则④：一键关闭）。"""
    settings = Settings()

    assert settings.cold_path_enabled is True
    assert settings.llm_provider == ""
    assert settings.llm_api_key == ""
    assert settings.llm_base_url == ""
    assert settings.llm_model == ""
    assert settings.llm_max_tokens_per_req == 4096
    assert settings.llm_max_concurrency == 4
    assert settings.llm_monthly_budget == 1_000_000
    assert settings.llm_request_timeout_s == 10.0


def test_cold_path_cost_guardrails_are_overridable(monkeypatch) -> None:
    """`WMS_` 前缀环境变量覆盖（env_prefix 机制）。"""
    monkeypatch.setenv("WMS_LLM_PROVIDER", "mock")
    monkeypatch.setenv("WMS_LLM_MAX_TOKENS_PER_REQ", "1024")
    monkeypatch.setenv("WMS_LLM_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("WMS_LLM_MONTHLY_BUDGET", "500")

    settings = Settings()

    assert settings.llm_provider == "mock"
    assert settings.llm_max_tokens_per_req == 1024
    assert settings.llm_max_concurrency == 2
    assert settings.llm_monthly_budget == 500
