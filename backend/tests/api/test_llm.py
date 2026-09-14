"""冷路径端点（`routes/llm.py`）的 API 冒烟 + 双产物响应契约测试（tasks.md 5.1 的验证）。

事实来源：openspec/changes/ai-assist/design.md D4（双产物）、D13（端点表）
          spec `ai-assist`「双产物响应契约」「冷路径开关守卫与一键关闭」

双产物契约：每个冷路径能力响应必含 `rule`（恒有、非空）/ `ai`（可选）/ `ai_generated`
（布尔）/ `degraded_reason`（可空），且不变量 `ai_generated == (degraded_reason is None)`。
Phase A `llm_provider=""` → 所有能力走「仅规则卡片」降级，故 `ai` 恒 `None`、
`ai_generated=false`、`degraded_reason` 非空 —— 但「规则算、LLM 只叙事」的红线由
`rule` 非空且数值正确体现，本套件逐端点断言。

鉴权 403 属 5.3，此处只冒烟「端点已接通、双产物形状对」；越权与边界归 6.1。
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.enums import AccountStatus, Role
from app.core.security import create_session_token
from app.models.identity import Account
from app.schemas.conversation import ConversationMessageResponse
from app.schemas.llm import AI_NOTICE, DualProductResponse
from tests.logic.conftest import AisleSpec, InventorySpec, MaterialSpec

pytestmark = pytest.mark.api


def _enable_cold_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启冷路径开关（默认关闭，端点守卫在请求时读 `settings.cold_path_enabled`）。"""
    monkeypatch.setattr(settings, "cold_path_enabled", True)


def _token_for(api, role: Role) -> str:
    """另建一个该角色的账号并签一份凭据（同 test_allocate.py 的 `_token_for`）。"""
    with api.factory() as session:
        account = Account(
            warehouse_id=settings.warehouse_code,
            username=f"gtj_{role.value}",
            password_hash="端点用例不验口令（见 api 夹具的说明）",
            role=role,
            status=AccountStatus.ACTIVE,
        )
        session.add(account)
        session.commit()
        account_id = account.id
    return create_session_token(account_id, role.value, AccountStatus.ACTIVE.value)


def _headers_for(api, role: Role) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token_for(api, role)}"}


def _assert_dual_product(body: dict) -> None:
    """双产物响应契约：四字段齐备 + 不变量 `ai_generated == (degraded_reason is None)`。"""
    assert set(body) >= {"rule", "ai", "ai_generated", "degraded_reason"}
    assert isinstance(body["rule"], dict) and body["rule"], "rule 恒有且非空"
    assert isinstance(body["ai_generated"], bool)
    assert (body["ai_generated"] is True) == (body["degraded_reason"] is None)


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


def test_toggle_admin_flips_switch(job_api, monkeypatch) -> None:
    """管理员经 `POST /api/llm/toggle` 开/关开关，运行时落点到 `settings.cold_path_enabled`。"""
    monkeypatch.setattr(settings, "cold_path_enabled", False)
    admin = _headers_for(job_api, Role.ADMIN)

    on = job_api.client.post("/api/llm/toggle", json={"enabled": True}, headers=admin)
    assert on.status_code == 200
    assert on.json()["cold_path_enabled"] is True
    assert settings.cold_path_enabled is True

    off = job_api.client.post("/api/llm/toggle", json={"enabled": False}, headers=admin)
    assert off.status_code == 200
    assert off.json()["cold_path_enabled"] is False
    assert settings.cold_path_enabled is False


def test_kpi_interpret_dual_product_contract(job_api, monkeypatch) -> None:
    """① KPI 解读（只读）→ 双产物，provider 空 → `provider_unconfigured` 降级。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": settings.warehouse_code, "period": "2026-09"},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    _assert_dual_product(body)
    assert "metrics" in body["rule"]
    assert body["ai"] is None
    assert body["ai_generated"] is False
    assert body["degraded_reason"] == "provider_unconfigured"


def test_deviation_attribute_dual_product_contract(job_api, monkeypatch) -> None:
    """② 偏离归因（只读）→ 双产物；无偏离数据仍产出空证据清单（规则卡片不报错）。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/llm/deviation/attribute",
        json={"warehouse_id": settings.warehouse_code, "material_code": "M1"},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    _assert_dual_product(body)
    assert body["rule"]["material_code"] == "M1"
    assert body["degraded_reason"] == "provider_unconfigured"


def test_weight_tune_insufficient_samples_degraded(job_api, monkeypatch) -> None:
    """③ 权重调优：历史批次 < 50 → `insufficient_samples` 降级，不调 LLM。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/llm/weight/tune",
        json={"warehouse_id": settings.warehouse_code},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    _assert_dual_product(body)
    assert body["rule"]["metrics"]["sample_size"] == 0
    assert body["degraded_reason"] == "insufficient_samples"


def test_relocate_propose_dual_product_contract(job_api, monkeypatch) -> None:
    """④ 移库方案（只读试算）→ 双产物，规则算多方案 + provider 空降级，不产生 JobOrder。"""
    _enable_cold_path(monkeypatch)
    _seed_material_across_aisles(job_api)
    response = job_api.client.post(
        "/api/llm/relocate/propose",
        json={"warehouse_id": settings.warehouse_code, "material_code": "M1"},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    body = response.json()
    _assert_dual_product(body)
    assert [p["name"] for p in body["rule"]["metrics"]["plans"]] == ["激进", "均衡", "保守"]
    assert body["degraded_reason"] == "provider_unconfigured"
    # 只试算不落库（红线 2：未确认不产生 JobOrder）。
    assert job_api.orders() == ()


def test_weight_apply_endpoint_wired(job_api, monkeypatch) -> None:
    """③ 采纳落地端点已接通（规则侧 `apply_weight`）：建议不存在 → 404 `not_found`。"""
    _enable_cold_path(monkeypatch)
    response = job_api.client.post(
        "/api/llm/weight/apply",
        json={"warehouse_id": settings.warehouse_code, "suggestion_id": 999_999},
        headers=job_api.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# ------------------------------------------------------------------ AI Notice（tasks.md 5.2）

def test_ai_notice_is_forced_on_dual_product_serialization() -> None:
    """AI Notice 强制注入（spec「AI 建议标注」）：非空 `ai` 序列化必带标注前缀。"""
    response = DualProductResponse(
        rule={"metrics": {}},
        ai="集中度 80% 落在 3 巷",
        ai_generated=True,
        degraded_reason=None,
    )
    assert response.model_dump()["ai"] == f"{AI_NOTICE}\n集中度 80% 落在 3 巷"


def test_ai_notice_is_forced_on_conversation_serialization() -> None:
    """对话台响应的 `ai` 走同一契约：非空叙事同样强制注入标注。"""
    response = ConversationMessageResponse(
        intent="KPI_INTERPRET",
        write_intent=False,
        rule={"metrics": {}},
        ai="本周集中度向好",
        ai_generated=True,
        degraded_reason=None,
    )
    assert response.model_dump()["ai"] == f"{AI_NOTICE}\n本周集中度向好"


def test_ai_notice_skips_none() -> None:
    """降级无叙事（`ai=None`）→ 不注入标注（没有叙事就没有标注，前端无标注不渲染）。"""
    response = DualProductResponse(
        rule={"metrics": {}},
        ai=None,
        ai_generated=False,
        degraded_reason="provider_unconfigured",
    )
    assert response.model_dump()["ai"] is None
