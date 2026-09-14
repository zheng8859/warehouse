"""L3 性能基线（4 道）：单物料评分 / cap 查询 / 落位写入 / 大队列批量分配 P95。

对应 golden `golden_053`（单物料评分 ≤1s）、`golden_054`（cap 查询 ≤100ms）、
`golden_056`（落位写入 ≤200ms）、`golden_058`（大队列批量分配 P95 < 3000ms）。

性能 SLA 见 CLAUDE.md §十：解析 ≤5min/文件 · cap 查询 ≤100ms · 单物料评分 ≤1s ·
落位写入 ≤200ms · 后验 ≤1s · 看板刷新 ≤3s。golden_053/058 标记 `slow`（仅 `--run-slow`
纳入）；054/056 常规纳入。阈值是**上界**，本机远超，断言只挡「量级退化」（如 O(n²)）。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.core.enums import JobStatus, JobType
from app.models.linkage import AisleCap
from tests.logic.conftest import AisleSpec, JobOrderSpec, MaterialSpec, make_scenario

pytestmark = pytest.mark.l3

WAREHOUSE = "GTJ10036"
ALLOCATE_URL = "/api/allocate/batch"
CONFIRM_URL = "/api/job/batch/confirm"


def _elapsed_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _p95(latencies: list[float]) -> float:
    ordered = sorted(latencies)
    idx = min(int(0.95 * len(ordered)), len(ordered) - 1)
    return ordered[idx]


@pytest.mark.slow
def test_golden_053_single_material_scoring_within_1s(eval_api):
    """golden_053：单物料评分（批量分配的评分→选道）耗时 ≤1s。"""
    scenario = eval_api.seed(
        aisles=[
            AisleSpec("01", cap_total=100, is_near_station=True),
            AisleSpec("02", cap_total=100, is_near_station=False),
        ],
        materials=[MaterialSpec("M1", abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(order_no="PO-1", material_code="M1", qty=10, batch_no="B1", job_type=JobType.INBOUND),
        ],
    )
    order_id = str(scenario.job_orders[0].id)

    t0 = time.perf_counter()
    resp = eval_api.client.post(
        ALLOCATE_URL,
        json={"warehouse_id": WAREHOUSE, "job_order_ids": [order_id]},
        headers=eval_api.headers(),
    )
    elapsed = _elapsed_ms(t0)

    assert resp.status_code == 200, resp.text
    assert elapsed < 1000


def test_golden_054_cap_query_within_100ms(session):
    """golden_054：cap 查询耗时 ≤100ms。"""
    make_scenario(session, aisles=[AisleSpec(f"{i:02d}", cap_total=100) for i in range(1, 6)])

    t0 = time.perf_counter()
    rows = list(session.scalars(select(AisleCap)))
    elapsed = _elapsed_ms(t0)

    assert len(rows) >= 5
    assert elapsed < 100


def test_golden_056_placement_write_within_200ms(eval_api):
    """golden_056：落位写入（确认 → 写台账 + cap 增量）耗时 ≤200ms。"""
    scenario = eval_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-1", material_code="M1", qty=40, batch_no="B1",
                job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    order_id = str(scenario.job_orders[0].id)

    t0 = time.perf_counter()
    resp = eval_api.client.post(
        CONFIRM_URL,
        json={
            "warehouse_id": WAREHOUSE,
            "orders": [{"job_order_id": order_id, "source_location_code": None, "target_location_code": "010104"}],
        },
        headers=eval_api.headers(),
    )
    elapsed = _elapsed_ms(t0)

    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == JobStatus.VERIFIED.value
    assert elapsed < 200


@pytest.mark.slow
def test_golden_058_large_queue_batch_allocation_p95_within_threshold(eval_api):
    """golden_058：大队列批量分配 P95 耗时 < 3000ms（性能 SLA）。"""
    materials = [MaterialSpec(f"M{i}", abc_class="A", material_name=f"料{i}") for i in range(10)]
    aisles = [AisleSpec(f"{i:02d}", cap_total=100000, is_near_station=(i == 1)) for i in range(1, 6)]
    orders = [
        JobOrderSpec(
            order_no=f"PO-{i:03d}", material_code=f"M{i % 10}", qty=5,
            batch_no=f"B{i:03d}", job_type=JobType.INBOUND,
        )
        for i in range(100)
    ]
    scenario = eval_api.seed(materials=materials, aisles=aisles, job_orders=orders)
    ids = [str(o.id) for o in scenario.job_orders]

    latencies: list[float] = []
    for oid in ids:
        t0 = time.perf_counter()
        resp = eval_api.client.post(
            ALLOCATE_URL,
            json={"warehouse_id": WAREHOUSE, "job_order_ids": [oid]},
            headers=eval_api.headers(),
        )
        assert resp.status_code == 200, resp.text
        latencies.append(_elapsed_ms(t0))

    assert _p95(latencies) < 3000
