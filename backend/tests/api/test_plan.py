"""只读理由端点 `GET /api/plan/{plan_id}` 的契约测试（tasks.md 1.4）。

事实来源：openspec/changes/inbound-domain/design.md D8（`warehouse_id` 隔离、透传
          `payload_json`、未知/跨仓对齐 `_load_order` 的 `ValidationBlocked` 422）
          spec `recommendation-engine`「推荐理由读」（2 场景）

## 造数口径

理由体只有一个来源（库里的 `RecommendationPlan.payload_json`，`allocate.py` D10
「理由不随 `plans[]` 返回、按 `plan_id` 另取」）。故「按方案取理由」走真实分配
（`POST /api/allocate/batch`）产出方案，再按 `plan_id` 下钻 —— 与操作员在 p3 上的
路径一致，同时钉住「响应里的 `plan_id` 指回库里那一行」。
"""
from __future__ import annotations

import pytest

from app.models.job import RecommendationPlan
from app.schemas.reason import FACTOR_NAMES
from tests.api.conftest import Api
from tests.logic.conftest import AisleSpec, JobOrderSpec, MaterialSpec

pytestmark = pytest.mark.api

WAREHOUSE = "GTJ10036"
OTHER_WAREHOUSE = "GTJ10023"
MATERIAL = "MOK"


def _allocate_one(job_api: Api) -> tuple[str, str]:
    """分配一张入库单，返回 `(job_order_id, plan_id)`（都是 `str` 形态）。"""
    scenario = job_api.seed(
        aisles=[AisleSpec("01", cap_total=100, is_near_station=True, station_weight=1.0)],
        materials=[MaterialSpec(MATERIAL, abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-20260908-001",
                material_code=MATERIAL,
                qty=10,
                abc_class="A",
                batch_no="B26090801",
            ),
        ],
    )
    order_id = str(scenario.job_orders[0].id)

    response = job_api.client.post(
        "/api/allocate/batch",
        json={"warehouse_id": WAREHOUSE, "job_order_ids": [order_id]},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    plan = response.json()["plans"][0]
    return order_id, str(plan["plan_id"])


def test_plan_read_returns_the_stored_reason_payload(job_api: Api) -> None:
    """按 `plan_id` 取回该方案的 6 因子理由与降级标记，且与库里那一份逐字一致。"""
    order_id, plan_id = _allocate_one(job_api)

    response = job_api.client.get(
        f"/api/plan/{plan_id}",
        params={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 200
    payload = response.json()

    # 理由体指向分配它的那张单，六项因子齐（spec「6 因子理由」）。
    assert payload["job_id"] == order_id
    assert sorted(payload["factors"]) == sorted(FACTOR_NAMES)
    # 降级标记可查（spec「降级标记」）：`degraded` 是布尔，`degrade_reason` 键在。
    assert isinstance(payload["degraded"], bool)
    assert "degrade_reason" in payload

    # 透传 = 与库里存的那一份逐字一致（D8：理由体只有一个来源，不复制副本）。
    with job_api.factory() as session:
        stored = session.get(RecommendationPlan, int(plan_id)).payload_json
    assert payload == stored


def test_plan_read_of_unknown_plan_rejects(job_api: Api) -> None:
    """未知 `plan_id` ⇒ 422，不返回理由内容。"""
    response = job_api.client.get(
        "/api/plan/999999",
        params={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"


def test_plan_read_of_cross_warehouse_plan_rejects(job_api: Api) -> None:
    """跨仓 = 未知：方案在本仓存在，但用他仓 `warehouse_id` 取 ⇒ 422，不泄露存在性。"""
    _, plan_id = _allocate_one(job_api)

    response = job_api.client.get(
        f"/api/plan/{plan_id}",
        params={"warehouse_id": OTHER_WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"


def test_plan_read_of_non_canonical_id_rejects(job_api: Api) -> None:
    """`plan_id` 形状不合法（非十进制正整数）⇒ 422（`_ID_PATTERN` 与 `_load_order` 同一口径）。"""
    response = job_api.client.get(
        "/api/plan/007",
        params={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
