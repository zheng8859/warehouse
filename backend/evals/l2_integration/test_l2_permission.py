"""L2 权限守卫与安全隔离（7 道）：401 / 403 / 422 / 404 + 核心链路出域=0 + 脱敏 + 开关。

对应 golden `golden_019`（未认证 401）、`golden_020`（无权限 403）、`golden_021`（报文错 422）、
`golden_022`（核心链路出域事件 = 0，P0）、`golden_023`（冷路径外发前脱敏不残留敏感字段）、
`golden_024`（冷路径开关关闭 → 外部调用 = 0）。

三层检查（13 §6.1）：认证中间件（401）→ `require_permission`（403，`inbound.operate` 仅
仓管员/管理员）→ 操作确认。`golden_022` 钉「评分 / 落位 / 台账全部本地确定性计算，不依赖
外部 LLM」—— 推荐理由是规则算出的数值分解，不含 AI 叙事（琥珀色「AI 建议」只在冷路径注入）。
"""
from __future__ import annotations

import json

import pytest

from app.core.enums import JobStatus, JobType, Role
from evals.eval_utils import assert_no_pii
from tests.logic.conftest import DEFAULT_WEIGHTS, AisleSpec, JobOrderSpec, MaterialSpec

pytestmark = pytest.mark.l2

WAREHOUSE = "GTJ10036"
ALLOCATE_URL = "/api/allocate/batch"

MATERIAL = "MOK"


def _allocate(client, headers, job_order_ids: list[str]):
    return client.post(
        ALLOCATE_URL,
        json={"warehouse_id": WAREHOUSE, "job_order_ids": job_order_ids},
        headers=headers,
    )


def test_golden_019_missing_credentials_return_401(eval_api):
    """golden_019：未携带凭据 → 401（中间件全局覆盖，端点整个没跑）。"""
    resp = _allocate(eval_api.client, {}, ["1"])

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthenticated"


def test_golden_020_planner_lacking_inbound_operate_gets_403(eval_api):
    """golden_020：计划员无 `inbound.operate` → 批量分配 403（资源级鉴权）。"""
    scenario = eval_api.seed(
        job_orders=[
            JobOrderSpec(order_no="PO-01", material_code=MATERIAL, qty=10, job_type=JobType.INBOUND),
        ],
    )
    order_id = str(scenario.job_orders[0].id)

    resp = _allocate(eval_api.client, eval_api.headers(Role.PLANNER), [order_id])

    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_golden_021_malformed_body_returns_422(eval_api):
    """golden_021：报文错（非规范单号）→ 422，不落任何方案。"""
    resp = _allocate(eval_api.client, eval_api.headers(), ["abc"])

    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_blocked"


def test_unknown_import_session_returns_404(eval_api):
    """404：不存在的导入会话 → `not_found`。"""
    resp = eval_api.client.post(
        "/api/import/validate", json={"session_no": "IMP-NOPE"}, headers=eval_api.headers()
    )

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_golden_022_core_chain_has_zero_egress_and_no_ai_narrative(eval_api):
    """golden_022（P0）：核心链路（分配 → 确认 → 台账）全程本地确定性计算，出域事件 = 0。

    「出域 = 0」的可观察形态：推荐理由是规则算出的数值分解（`factors` = 库里的默认权重，
    `breakdown` 六因子取值），不含冷路径才注入的「AI 建议 / 仅供参考」叙事；确认后台账
    照常本地落一行 —— 整条链路不依赖任何外部 LLM。
    """
    scenario = eval_api.seed(
        aisles=[
            AisleSpec("01", cap_total=100, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec(MATERIAL, abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=10,
                abc_class="A", batch_no="B26090801", job_type=JobType.INBOUND,
            ),
        ],
    )
    order_id = str(scenario.job_orders[0].id)

    resp = _allocate(eval_api.client, eval_api.headers(), [order_id])

    assert resp.status_code == 200
    (plan,) = resp.json()["plans"]
    assert plan["aisles"] == ["01"]

    reason = eval_api.client.get(
        f"/api/plan/{plan['plan_id']}", params={"warehouse_id": WAREHOUSE}, headers=eval_api.headers()
    ).json()

    # 规则算出的理由：权重取自库里的默认配置，六因子分解是数值项，无 AI 叙事。
    assert reason["factors"] == dict(DEFAULT_WEIGHTS)
    dumped = json.dumps(reason, ensure_ascii=False)
    assert "AI 建议" not in dumped and "仅供参考" not in dumped

    # 确认 → 台账本地落一行，整条核心链路无外部依赖。
    confirm = eval_api.client.post(
        "/api/job/batch/confirm",
        json={
            "warehouse_id": WAREHOUSE,
            "orders": [{"job_order_id": order_id, "source_location_code": None, "target_location_code": "010104"}],
        },
        headers=eval_api.headers(),
    )
    assert confirm.status_code == 200
    assert confirm.json()["results"][0]["status"] == JobStatus.VERIFIED.value
    assert len(eval_api.ledgers()) == 1


def test_golden_023_redact_leaves_no_forbidden_fields():
    """golden_023：冷路径外发前脱敏 —— 脱敏后任一层级都不残留敏感字段。"""
    payload = {
        "material_code": "M1",
        "qty": 10,
        "order_no": "PO-001",        # 禁出：订单号
        "operator_name": "张三",      # 禁出：操作员姓名
        "metrics": {
            "customer_name": "X公司",  # 禁出：客户名（嵌套）
            "scores": {"price": 1.0},  # 禁出：价格（更深一层）
        },
    }
    out = assert_no_pii(payload)
    dumped = json.dumps(out, ensure_ascii=False)
    for forbidden in ("order_no", "operator_name", "customer_name", "price"):
        assert forbidden not in dumped, f"脱敏后仍残留禁出字段 {forbidden!r}"
    # 白名单字段保留、值不改写：脱敏只截留不该出的字段。
    assert out["material_code"] == "M1"
    assert out["qty"] == 10


def test_golden_024_cold_path_off_makes_zero_external_calls(eval_api):
    """golden_024：冷路径开关关闭 → 端点先 409 守卫，外部 LLM 调用 = 0。"""
    resp = eval_api.client.post(
        "/api/llm/kpi/interpret",
        json={"warehouse_id": WAREHOUSE},
        headers=eval_api.headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error"] == "cold_path_disabled"
