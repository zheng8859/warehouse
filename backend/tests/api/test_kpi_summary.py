"""KPI 看板聚合端点 `GET /api/kpi/summary` 的契约测试。

事实来源：18-KPI 设计 §1.3（四个看板指标）；spec `kpi-summary`（从真实数据计算，不硬编码）。

钉住：四个指标从真实数据聚合、不是硬编码演示值；无数据时各项为 0 不报错。
"""
from __future__ import annotations

import pytest

from tests.logic.conftest import InventorySpec, MaterialSpec

pytestmark = pytest.mark.api

URL = "/api/kpi/summary"
WAREHOUSE_ID = "GTJ10036"


def _summary(job_api) -> "object":
    return job_api.client.get(
        f"{URL}?warehouse_id={WAREHOUSE_ID}", headers=job_api.headers
    )


def test_kpi_summary_computes_cross_aisle_mean_from_inventory(job_api) -> None:
    """同物料跨巷道均值从库存取值：M1 6 巷 + M2 2 巷 → 4.0，其余无数据项为 0。"""
    job_api.seed(
        materials=[MaterialSpec("M1"), MaterialSpec("M2")],
        inventory=[
            *[InventorySpec(f"0{i}0101", "M1", "B1", 10) for i in range(1, 7)],  # M1 6 巷
            InventorySpec("010101", "M2", "B2", 10),
            InventorySpec("020101", "M2", "B2", 10),
        ],
    )

    response = _summary(job_api)

    assert response.status_code == 200
    body = response.json()
    assert body["material_cross_aisle_mean"] == 4.0
    assert body["weighted_concentration"] == 0.0
    assert body["per_do_cross_aisle_mean"] == 0.0
    assert body["adoption_rate"] == 0.0


def test_kpi_summary_empty_data_returns_zeros(job_api) -> None:
    """无快照 / 无台账 / 无入库推荐 → 四项全 0，不报错（不硬编码演示值）。"""
    response = _summary(job_api)

    assert response.status_code == 200
    assert response.json() == {
        "material_cross_aisle_mean": 0.0,
        "weighted_concentration": 0.0,
        "per_do_cross_aisle_mean": 0.0,
        "adoption_rate": 0.0,
    }
