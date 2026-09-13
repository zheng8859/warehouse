"""批量确认端点 `POST /api/job/batch/confirm` 的契约测试（tasks.md 6.1 的验证）。

事实来源：design.md D6（端点清单）、D7（逐单独立提交）
          spec `transaction-base`「作业单写操作端点」（场景：批量确认逐单独立事务）
          17-数据模型设计 §4.3（台账）、§4.4（后验）
          CLAUDE.md §四（未确认不产生台账 / cap 与台账同事务）

## 本文件钉住的那一件事：逐单独立事务（D7）

与 `allocate` 的「整批一次提交」正好相反 —— 那边要的是「一半有方案一半没有的中间态
不存在」，这边要的是「**个别单失败回 `PLANNED` 不影响同批其余单**」。失败的两种形态：

1. **写台账失败**（无生产批号）→ 编排**就地**退回 `PLANNED`，不抛异常。这是正常分支，
   该单的 `PLANNED` 照常 `commit()`。
2. **源状态非 `PLANNED`** → `StateConflict` 抛到端点，端点**只记这一单**、继续同批其余单。

两条都验「这一单的成败，不滚掉同批另一单已经写下的台账 / cap 增量」—— 那是 D7 的
「逐单独立提交」在端点层的可观察形态。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType
from tests.api.conftest import Api
from tests.logic.conftest import JobOrderSpec

pytestmark = pytest.mark.api

CONFIRM_URL = "/api/job/batch/confirm"
MATERIAL = "M1"
BATCH = "B26090801"


def _confirm_body(orders: list[dict], *, warehouse_id: str = "GTJ10036") -> dict:
    return {"warehouse_id": warehouse_id, "orders": orders}


def _inbound_item(
    order_id: str | int, *, target: str = "010104"
) -> dict:
    return {
        "job_order_id": str(order_id),
        "source_location_code": None,
        "target_location_code": target,
    }


# ------------------------------------------------------------------ 逐单独立事务（写台账失败回 PLANNED）

def test_a_write_failure_returns_to_planned_without_affecting_the_rest(job_api: Api) -> None:
    """两张单同一批：一张无批号（写台账失败回 `PLANNED`）、一张正常（`VERIFIED`），互不影响。

    「互不影响」的判据分两半：失败单的 `PLANNED` **确实提交了**（不是被同批整体回滚）；
    成功单的台账 / 后验**照常写下了**（没有被失败单拖下水）。任一单的成败都不滚掉另一单。
    """
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=40,
                batch_no=None, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
            JobOrderSpec(
                order_no="PO-02", material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    id_fail, id_ok = (str(order.id) for order in scenario.job_orders)

    response = job_api.client.post(
        CONFIRM_URL,
        json=_confirm_body([_inbound_item(id_fail), _inbound_item(id_ok)]),
        headers=job_api.headers,
    )

    assert response.status_code == 200
    results = {item["job_order_id"]: item for item in response.json()["results"]}
    assert results[id_fail]["status"] == JobStatus.PLANNED.value
    assert results[id_fail]["error"] is None, "写台账失败回 PLANNED 是正常分支，不是错误"
    assert results[id_ok]["status"] == JobStatus.VERIFIED.value

    # 失败单：状态已提交为 PLANNED、零台账、零后验
    by_id = {order.id: order for order in job_api.orders()}
    assert by_id[int(id_fail)].status is JobStatus.PLANNED
    assert by_id[int(id_ok)].status is JobStatus.VERIFIED
    assert [lg.job_order_id for lg in job_api.ledgers()] == [int(id_ok)], (
        "失败单不该写出台账；成功单的台账照常在"
    )
    assert {v.job_order_id for v in job_api.verifications()} == {int(id_ok)}


def test_a_non_planned_order_is_rejected_per_order_and_the_rest_still_confirm(
    job_api: Api,
) -> None:
    """源状态非 `PLANNED`（`PENDING`）的单被**逐单拒绝**（记 `error`），同批其余单照常确认。

    这是 D7 与 `allocate`「整批拒绝」的分野：`allocate` 里一张非 `PENDING` 的单让**整批**
    409；这里一张非 `PLANNED` 的单只让**这一单**落 `error`，其余单仍然 `VERIFIED`。
    """
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PENDING,
            ),
            JobOrderSpec(
                order_no="PO-02", material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    id_pending, id_ok = (str(order.id) for order in scenario.job_orders)

    response = job_api.client.post(
        CONFIRM_URL,
        json=_confirm_body([_inbound_item(id_pending), _inbound_item(id_ok)]),
        headers=job_api.headers,
    )

    assert response.status_code == 200
    results = {item["job_order_id"]: item for item in response.json()["results"]}
    assert results[id_pending]["status"] == JobStatus.PENDING.value
    assert results[id_pending]["error"] is not None
    assert results[id_ok]["status"] == JobStatus.VERIFIED.value

    by_id = {order.id: order for order in job_api.orders()}
    assert by_id[int(id_pending)].status is JobStatus.PENDING
    assert by_id[int(id_ok)].status is JobStatus.VERIFIED
    assert [lg.job_order_id for lg in job_api.ledgers()] == [int(id_ok)]


# ------------------------------------------------------------------ 报文级整批拒绝

def test_an_unknown_order_rejects_the_whole_batch(job_api: Api) -> None:
    """未知单号 ⇒ 422 整批拒绝，零台账（报文错在确认循环**之前**就整批拦掉）。"""
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    (real_id,) = (str(order.id) for order in scenario.job_orders)

    response = job_api.client.post(
        CONFIRM_URL,
        json=_confirm_body([_inbound_item(real_id), _inbound_item("999999")]),
        headers=job_api.headers,
    )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert job_api.ledgers() == (), "被拒的请求不得留下任何台账"


def test_a_malformed_location_field_rejects_the_whole_batch(job_api: Api) -> None:
    """入库单填了 `source_location_code`（矩阵只允许目标）⇒ 422，零台账。

    DB 的 `_LEDGER_LOCATION_CHECK` 会把这类「填错格」拦成 500；端点按 `job_type` 先判成
    422，让「入库填了源库位」这类错当场指回调用方。
    """
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    (order_id,) = (str(order.id) for order in scenario.job_orders)

    body = _confirm_body(
        [{"job_order_id": order_id, "source_location_code": "010104",
          "target_location_code": None}]
    )
    response = job_api.client.post(CONFIRM_URL, json=body, headers=job_api.headers)

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert job_api.ledgers() == ()
