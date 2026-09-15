"""对话台统一入口 `POST /api/conversation/message` 的 API 契约测试（tasks.md 4.2 的验证）。

事实来源：openspec/changes/ai-assist/design.md D9（L0/L2 意图 + write_intent）、D13（端点表）
          spec `ai-assist`「对话台 L0/L2 意图识别」

核心不变量：写意图（③④）返回建议 + `write_intent=true` 且零台账（`Ledger` / `JobOrder`
都不产生）；读意图（①②）直出结果 + `write_intent=false`。写意图走「影子模式 / 试算」，
落地复用 `weight/apply` 与 `relocate.operate` 二次确认（红线 2）。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from tests.logic.conftest import AisleSpec, InventorySpec, MaterialSpec

pytestmark = pytest.mark.api


def _enable_cold_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启冷路径开关（默认打开，此处显式置位；守卫在请求时读 `settings.cold_path_enabled`）。"""
    monkeypatch.setattr(settings, "cold_path_enabled", True)


def _seed_material_across_aisles(job_api) -> None:
    """造一段「料号 M1 分散在 8 巷」的场景，供 ④ 移库方案生成。"""
    job_api.seed(
        aisles=[AisleSpec(aisle_no=f"{i:02d}", cap_total=100) for i in range(1, 9)],
        materials=[MaterialSpec(material_code="M1", abc_class="A")],
        inventory=[
            InventorySpec(
                location_code=f"{i:02d}0101", material_code="M1", batch_no="B1", qty=60 - i
            )
            for i in range(1, 9)
        ],
    )


def test_write_intent_returns_suggestion_and_zero_ledger(job_api, monkeypatch) -> None:
    """写意图（④ 移库方案）返回建议 + `write_intent=true`，且不产生台账 / 作业单。"""
    _enable_cold_path(monkeypatch)
    _seed_material_across_aisles(job_api)

    response = job_api.client.post(
        "/api/conversation/message",
        json={
            "warehouse_id": settings.warehouse_code,
            "intent": "RELOCATE_PROPOSE",
            "slots": {"material_code": "M1"},
        },
        headers=job_api.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "RELOCATE_PROPOSE"
    assert body["write_intent"] is True
    assert [p["name"] for p in body["rule"]["metrics"]["plans"]] == ["激进", "均衡", "保守"]
    # provider 未配置 → 降级，仍有规则卡片（双产物契约）。
    assert body["ai_generated"] is False
    assert body["degraded_reason"] is not None

    # 零台账：写意图只回建议，不产生 Ledger / JobOrder（红线 2）。
    assert job_api.ledgers() == ()
    assert job_api.orders() == ()


def test_read_intent_returns_direct_result(job_api, monkeypatch) -> None:
    """读意图（① KPI 解读）直出结果 + `write_intent=false`。"""
    _enable_cold_path(monkeypatch)

    response = job_api.client.post(
        "/api/conversation/message",
        json={
            "warehouse_id": settings.warehouse_code,
            "intent": "KPI_INTERPRET",
            "slots": {"period": "2026-09"},
        },
        headers=job_api.headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "KPI_INTERPRET"
    assert body["write_intent"] is False
    assert "metrics" in body["rule"]


def test_cold_path_disabled_returns_409(job_api, monkeypatch) -> None:
    """开关显式关闭 → 409 `cold_path_disabled`（不调 LLM、不路由、不落日志）。"""
    monkeypatch.setattr(settings, "cold_path_enabled", False)
    response = job_api.client.post(
        "/api/conversation/message",
        json={"warehouse_id": settings.warehouse_code, "intent": "KPI_INTERPRET"},
        headers=job_api.headers,
    )
    assert response.status_code == 409
    assert response.json()["error"] == "cold_path_disabled"


def test_l0_and_l2_are_mutually_exclusive(job_api, monkeypatch) -> None:
    """`intent` 与 `question` 只能给一个，同给 → 422。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/conversation/message",
        json={
            "warehouse_id": settings.warehouse_code,
            "intent": "KPI_INTERPRET",
            "question": "上周集中度怎么样",
        },
        headers=job_api.headers,
    )
    assert response.status_code == 422


def test_missing_material_code_for_write_intent(job_api, monkeypatch) -> None:
    """②④ 要 `material_code`，缺失 → 422（fail-closed，不静默直出）。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/conversation/message",
        json={"warehouse_id": settings.warehouse_code, "intent": "RELOCATE_PROPOSE"},
        headers=job_api.headers,
    )
    assert response.status_code == 422


def test_l2_free_text_unconfigured_provider_returns_provider_unconfigured(job_api, monkeypatch) -> None:
    """L2 自由文本 + provider 未配置 → `provider_unconfigured`（不是 `intent_unrecognized`，各归各）。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/conversation/message",
        json={"warehouse_id": settings.warehouse_code, "question": "帮我分析本周集中度下滑原因"},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] is None
    assert body["write_intent"] is False
    assert body["rule"] == {}
    assert body["ai"] is None
    assert body["ai_generated"] is False
    assert body["degraded_reason"] == "provider_unconfigured"


def test_l2_free_text_routes_to_kpi_interpret_when_nlu_recognizes(job_api, monkeypatch) -> None:
    """L2 自由文本被 NLU 识别为 KPI_INTERPRET → 端点正常路由到 ① 解读（本次修复的正向验证）。"""
    _enable_cold_path(monkeypatch)
    from app.llm.nlu import NluResult

    monkeypatch.setattr(
        "app.api.routes.conversation.recognize_intent",
        lambda question, *, settings=None, timeout_s=None: NluResult(
            intent="KPI_INTERPRET", slots={}, degraded_reason=None
        ),
    )

    response = job_api.client.post(
        "/api/conversation/message",
        json={"warehouse_id": settings.warehouse_code, "question": "帮我分析本周集中度下滑原因"},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "KPI_INTERPRET"
    assert body["write_intent"] is False
    assert "metrics" in body["rule"]
