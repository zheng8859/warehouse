"""只读队列端点 `GET /api/jobs` 的契约测试（tasks.md 1.3）。

事实来源：openspec/changes/inbound-domain/design.md D1（`type` 必填 `JobType` 枚举）、
          D3（`status`/`material_code`/`abc_class` 精确、`order_no` 前缀）、
          D4（纯 `SELECT`、不迁状态、跨仓靠 `warehouse_id` 进查询）
          spec `transaction-base`「入库作业队列查询」（5 场景）

## 造数纪律

- **同仓多单一次 `seed()`**：`make_scenario` 默认建一版 `Snapshot(version_no=1)`，而
  `Snapshot` 有 `(warehouse_id, version_no)` 唯一约束（`linkage.py`），同一用例内两次
  `seed()` 同仓会撞键。跨仓那条用不同 `warehouse_id`，键不同，可以分两次 seed。
- **`type=INBOUND` 大写**：`JobType` 是大小写敏感的 `str, Enum`，Pydantic v2 按 `.value`
  （`"INBOUND"`）匹配，小写 `inbound` 在参数层即 422 —— 与 `status=PLANNED` /
  `abc_class=A` 同一口径。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType
from tests.api.conftest import Api
from tests.logic.conftest import JobOrderSpec

pytestmark = pytest.mark.api

MATERIAL = "M1"
WAREHOUSE = "GTJ10036"
OTHER_WAREHOUSE = "GTJ10023"


def _get_jobs(job_api: Api, **params: str) -> list[dict]:
    response = job_api.client.get(
        "/api/jobs",
        params={"warehouse_id": WAREHOUSE, "type": "INBOUND", **params},
        headers=job_api.headers,
    )
    assert response.status_code == 200
    return response.json()


def test_jobs_read_filters_by_type(job_api: Api) -> None:
    """`type=INBOUND` 只返回入库单，混入的出库单不返回。"""
    job_api.seed(
        job_orders=[
            JobOrderSpec(order_no="PO-IN", material_code=MATERIAL, qty=40, job_type=JobType.INBOUND),
            JobOrderSpec(order_no="DO-OUT", material_code=MATERIAL, qty=10, job_type=JobType.OUTBOUND),
        ],
    )

    rows = _get_jobs(job_api)

    assert [row["order_no"] for row in rows] == ["PO-IN"]


def test_jobs_read_filters_by_status(job_api: Api) -> None:
    """`status=PLANNED` 只返回已规划的入库单。"""
    job_api.seed(
        job_orders=[
            JobOrderSpec(order_no="PO-PENDING", material_code=MATERIAL, qty=40, status=JobStatus.PENDING),
            JobOrderSpec(order_no="PO-PLANNED", material_code=MATERIAL, qty=40, status=JobStatus.PLANNED),
        ],
    )

    rows = _get_jobs(job_api, status="PLANNED")

    assert [row["order_no"] for row in rows] == ["PO-PLANNED"]


def test_jobs_read_is_warehouse_scoped(job_api: Api) -> None:
    """跨仓隔离：他仓的单不返回，本仓的单照常返回。"""
    job_api.seed(job_orders=[JobOrderSpec(order_no="PO-HOME", material_code=MATERIAL, qty=40)])
    job_api.seed(
        warehouse_id=OTHER_WAREHOUSE,
        job_orders=[JobOrderSpec(order_no="PO-OTHER", material_code=MATERIAL, qty=10)],
    )

    rows = _get_jobs(job_api)

    assert [row["order_no"] for row in rows] == ["PO-HOME"]


def test_jobs_read_does_not_mutate_state(job_api: Api) -> None:
    """查询不改变状态：`PENDING` 保持 `PENDING`，`lock_version` 不变。"""
    job_api.seed(job_orders=[JobOrderSpec(order_no="PO-A", material_code=MATERIAL, qty=40)])
    before = job_api.orders()[0]

    _get_jobs(job_api)

    after = job_api.orders()[0]
    assert before.status is JobStatus.PENDING
    assert after.status is JobStatus.PENDING
    assert after.lock_version == before.lock_version


def test_jobs_read_includes_downstream_identifiers(job_api: Api) -> None:
    """响应含 `job_order_id`（`str(id)` 形态）与 `lock_version`，可直接喂下游写端点。"""
    scenario = job_api.seed(job_orders=[JobOrderSpec(order_no="PO-A", material_code=MATERIAL, qty=40)])
    order_id = str(scenario.job_orders[0].id)

    rows = _get_jobs(job_api)

    assert len(rows) == 1
    row = rows[0]
    assert row["job_order_id"] == order_id
    assert row["lock_version"] == 0
    assert row["status"] == JobStatus.PENDING.value
    assert row["material_code"] == MATERIAL
    assert row["qty"] == 40
    assert row["bulk_batch_no"] is None
