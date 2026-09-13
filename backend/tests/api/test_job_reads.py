"""验证读端点 `GET /api/ledger`、`GET /api/verification/{job_id}` 的契约测试（tasks.md 6.3 的验证）。

事实来源：design.md D6（验证读路径在 `/api` 顶层，不挂 `/api/job/*` 写前缀）
          spec `transaction-base`「台账与后验验证读」（场景：台账查询返回反向行 / 后验结果可查）
          17-数据模型设计 §4.3（台账含 `is_reversal` 反向行）、§4.4（后验一行一条指标）
          CLAUDE.md §四（台账一套、追加式、不删不改）

## 两个读端点各自钉住的口径

1. **`GET /api/ledger`**：返回**正常行 + 反向行**（`is_reversal` 区分），按 `id` 升序 ——
   「冲正不删台账，反向行与原正常行并存」是「不删历史台账」红线（`CLAUDE.md` §四）在
   读侧的可观察形态。走 confirm → void 全链路，而不是直接造两行 `Ledger`：反向行照抄
   正常行的这个事实，只能由真实冲正产生。
2. **`GET /api/verification/{job_id}`**：一行一条指标（入库两条：同物料 / 同批跨巷道），
   `metric_kind` / `actual_value` / `threshold_value` / `verify_result` 四列齐全 —— 后验
   结论「可查」指的是这四列都在，不是只给一个状态位。

读路径在 `/api` 顶层（`design.md` D6 明文），与写路径 `/api/job/*` 分开 —— 台账 / 后验
是独立资源，不是「某个作业单的子资源」。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType
from tests.api.conftest import Api
from tests.logic.conftest import JobOrderSpec

pytestmark = pytest.mark.api

MATERIAL = "M1"
BATCH = "B26090801"
WAREHOUSE = "GTJ10036"


def _confirm(job_api: Api, order_id: str) -> None:
    """确认一张入库单到 `VERIFIED`（台账 + cap 增量 + 两条入库后验落库）。"""
    response = job_api.client.post(
        "/api/job/batch/confirm",
        json={
            "warehouse_id": WAREHOUSE,
            "orders": [{"job_order_id": order_id, "target_location_code": "010104"}],
        },
        headers=job_api.headers,
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == JobStatus.VERIFIED.value


def _seed_planned(job_api: Api, *, order_no: str) -> str:
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no=order_no, material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    return str(scenario.job_orders[0].id)


# ------------------------------------------------------------------ 台账读（含反向行）

def test_ledger_read_returns_normal_and_reversal_rows(job_api: Api) -> None:
    """confirm → void 后 `GET /api/ledger` 返回 `[False, True]` 两行，反向行照抄正常行。"""
    order_id = _seed_planned(job_api, order_no="PO-01")
    _confirm(job_api, order_id)

    voided = job_api.client.post(
        f"/api/job/{order_id}/void",
        json={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )
    assert voided.status_code == 200

    response = job_api.client.get(
        "/api/ledger",
        params={"warehouse_id": WAREHOUSE, "job_order_id": order_id},
        headers=job_api.headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert [row["is_reversal"] for row in rows] == [False, True]

    normal, reversal = rows
    # 反向行照抄正常行的台账事实（取反的是 qty 符号，`is_reversal` 作标记）。
    assert normal["ledger_type"] == "INBOUND"
    assert normal["order_no"] == "PO-01"
    assert normal["material_code"] == MATERIAL
    assert normal["batch_no"] == BATCH
    assert normal["qty"] == 40
    assert normal["target_location_code"] == "010104"
    assert normal["source_location_code"] is None

    assert reversal["ledger_type"] == "INBOUND"
    assert reversal["material_code"] == MATERIAL
    assert reversal["batch_no"] == BATCH
    assert reversal["target_location_code"] == "010104"


def test_ledger_read_of_unknown_order_rejects(job_api: Api) -> None:
    """未知作业单 ⇒ 422（读与写共用同一套「单号形状 + 归属」校验）。"""
    response = job_api.client.get(
        "/api/ledger",
        params={"warehouse_id": WAREHOUSE, "job_order_id": "999999"},
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"


# ------------------------------------------------------------------ 后验读

def test_verification_read_returns_per_metric_rows(job_api: Api) -> None:
    """confirm 后 `GET /api/verification/{job_id}` 返回入库两条指标，四列齐全、结论可查。"""
    order_id = _seed_planned(job_api, order_no="PO-01")
    _confirm(job_api, order_id)

    response = job_api.client.get(
        f"/api/verification/{order_id}",
        params={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2, "入库后验两条：同物料跨巷道 + 同批跨巷道（17 §4.4）"

    by_metric = {row["metric_kind"]: row for row in rows}
    assert set(by_metric) == {"同物料跨巷道", "同批跨巷道"}

    # 确认后增量把该料放进「01」巷道，故同物料 / 同批跨巷道实测值均为 1；阈值各取
    # `15-00` §六的主 / 辅口径（同物料 ≤5、同批 ≤3）。PASS 表示后验结论「可查」，
    # 不是只回一个状态位。
    material = by_metric["同物料跨巷道"]
    assert material["actual_value"] == 1.0
    assert material["threshold_value"] == 5.0
    assert material["verify_result"] == "PASS"

    batch = by_metric["同批跨巷道"]
    assert batch["actual_value"] == 1.0
    assert batch["threshold_value"] == 3.0
    assert batch["verify_result"] == "PASS"


def test_verification_read_of_unknown_order_rejects(job_api: Api) -> None:
    """未知作业单 ⇒ 422（后验读同样走「单号形状 + 归属」校验）。"""
    response = job_api.client.get(
        "/api/verification/999999",
        params={"warehouse_id": WAREHOUSE},
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
