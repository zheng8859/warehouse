"""冷路径端点异常 / 边界套件（tasks.md 6.1 的验证）。

事实来源：spec `ai-assist`「冷路径开关守卫与一键关闭」「LLM 侧失败降级不报错」
          「成本护栏与熔断」
          openspec/changes/ai-assist/design.md D2（开关守卫）、D10（降级不报错）、D3（熔断）

四条边界逐条钉死：

1. 开关关闭 → 409 `cold_path_disabled`（含持有 `ai.*` 权限的仓管员，先答「功能没开」）。
2. LLM 失败（不可用 / 超时）→ **200** 降级 + 规则卡片，绝不 5xx（D10：降级不是故障）。
3. 月度预算耗尽 → 入口熔断，200 + 仅规则卡片（`budget_exhausted`），不调 LLM。
4. 权限越界 → 403（见 `test_llm.py` 的 5.3 套件，本文件不复述）。

Phase A `llm_provider=""` 下 provider_unconfigured 的降级已由 `test_llm.py` 的冒烟用例
覆盖；这里补「真实失败路径」（未知 provider → `llm_unavailable`、慢后端 → `llm_timeout`）
与「熔断」两条 —— 它们需要改写 `settings` 或预置 `AiCostQuota` 记账行，故单列一文件。
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from app.core.config import settings
from app.llm import client
from app.models.llm import AiCostQuota

pytestmark = pytest.mark.api


def _enable_cold_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启冷路径开关（默认关闭，端点守卫在请求时读 `settings.cold_path_enabled`）。"""
    monkeypatch.setattr(settings, "cold_path_enabled", True)


def test_cold_path_disabled_returns_409_for_llm_endpoints(job_api) -> None:
    """开关关闭（默认）→ 409 `cold_path_disabled`，即便持 `ai.assist` 的仓管员亦然。"""
    response = job_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": settings.warehouse_code},
        headers=job_api.headers,
    )
    assert response.status_code == 409
    assert response.json()["error"] == "cold_path_disabled"


def test_llm_unavailable_degrades_to_200(job_api, monkeypatch) -> None:
    """未知 provider（Phase A 未接线）→ 200 + `llm_unavailable` 规则卡片，不 5xx。"""
    _enable_cold_path(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "openai")  # 非空但未接线

    response = job_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": settings.warehouse_code},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ai"] is None
    assert body["ai_generated"] is False
    assert body["degraded_reason"] == "llm_unavailable"
    assert body["rule"]  # 规则卡片仍返回（降级不静默、不丢规则数）


def test_llm_timeout_degrades_to_200(job_api, monkeypatch) -> None:
    """慢后端超过 `llm_request_timeout_s` → 200 + `llm_timeout`，不 5xx。"""
    _enable_cold_path(monkeypatch)
    monkeypatch.setattr(settings, "llm_provider", "mock")  # 非空，走解析
    monkeypatch.setattr(settings, "llm_request_timeout_s", 0.01)

    def slow(prompt: str) -> str:
        time.sleep(0.3)
        return "晚到的回复"

    # 端点不暴露 backend 注入，改 `_resolve_backend` 让非空 provider 解析到慢后端。
    monkeypatch.setattr(client, "_resolve_backend", lambda provider: slow)

    response = job_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": settings.warehouse_code},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ai_generated"] is False
    assert body["degraded_reason"] == "llm_timeout"
    assert body["rule"]


def test_budget_exhausted_fuses_to_rule_only(job_api, monkeypatch) -> None:
    """月度预算耗尽 → 入口熔断，200 + 仅规则卡片（`budget_exhausted`），不调 LLM。"""
    _enable_cold_path(monkeypatch)
    with job_api.factory() as session:
        session.add(
            AiCostQuota(
                warehouse_id=settings.warehouse_code,
                period=datetime.now().strftime("%Y-%m"),
                tokens_consumed=settings.llm_monthly_budget,
            )
        )
        session.commit()

    response = job_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": settings.warehouse_code},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ai"] is None
    assert body["ai_generated"] is False
    assert body["degraded_reason"] == "budget_exhausted"
    assert body["rule"]  # 仅规则卡片
