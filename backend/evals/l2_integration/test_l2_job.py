"""L2 作业闭环（8 道）：JobOrder 状态机 + Ledger 写入 + 乐观锁 + 顺路取只读。

对应 golden `golden_007`（确认写台账）、`golden_010`（未二次确认不产生台账）、
`golden_011`（EXECUTED 后重复确认拦截）、`golden_008`（顺路取只读派生）、
`golden_036`（货未入库顺路取提示不阻断）、`golden_012`（快照缺失/过期出库阻断）。

场景可溯源 15 §3.1（状态机）、§6.2/§6.3（逐单处置与写台账）、CLAUDE.md §四（未确认
不产生台账 / 台账只有一套 / 快照缺失阻断）。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType
from tests.logic.conftest import InventorySpec, JobOrderSpec, MaterialSpec

pytestmark = pytest.mark.l2

WAREHOUSE = "GTJ10036"
CONFIRM_URL = "/api/job/batch/confirm"
PICK_URL = "/api/job/batch/pick-sequence"

MATERIAL = "M1"
BATCH = "B26090801"


def _confirm(client, headers, orders: list[dict]):
    return client.post(CONFIRM_URL, json={"warehouse_id": WAREHOUSE, "orders": orders}, headers=headers)


def _inbound_item(order_id, *, target: str = "010104", lock_version=None):
    item = {"job_order_id": str(order_id), "source_location_code": None, "target_location_code": target}
    if lock_version is not None:
        item["lock_version"] = lock_version
    return item


def _pick(client, headers, job_order_ids: list[str]):
    return client.post(
        PICK_URL, json={"warehouse_id": WAREHOUSE, "job_order_ids": job_order_ids}, headers=headers
    )


def _seed_planned(eval_api, *, batch_no=BATCH):
    """一张已排方案的入库单（写台账成功与否由 `batch_no` 决定）。"""
    scenario = eval_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-01", material_code=MATERIAL, qty=40, batch_no=batch_no,
                job_type=JobType.INBOUND, status=JobStatus.PLANNED,
            ),
        ],
    )
    return str(scenario.job_orders[0].id)


# ------------------------------------------------------------------ 确认 → 写台账


def test_golden_007_confirm_inbound_writes_ledger_and_verifies(eval_api):
    """golden_007：`PLANNED` 入库单确认 → 落位 → 写台账 + 后验 → `VERIFIED`，台账 1 行。"""
    order_id = _seed_planned(eval_api)

    resp = _confirm(eval_api.client, eval_api.headers(), [_inbound_item(order_id)])

    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert result["status"] == JobStatus.VERIFIED.value
    ledgers = eval_api.ledgers()
    assert len(ledgers) == 1
    assert ledgers[0].job_order_id == int(order_id)


def test_golden_010_reject_produces_no_ledger(eval_api):
    """golden_010：`PLANNED` 单驳回（未二次确认）→ `REJECTED`，零台账。"""
    order_id = _seed_planned(eval_api)

    resp = eval_api.client.post(
        f"/api/job/{order_id}/reject",
        json={"warehouse_id": WAREHOUSE, "reason": "计划变更"},
        headers=eval_api.headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == JobStatus.REJECTED.value
    assert eval_api.ledgers() == (), "未二次确认不产生台账"


def test_golden_011_reconfirm_verified_order_is_blocked_without_duplicate_ledger(eval_api):
    """golden_011：`VERIFIED` 单重复确认 → 状态机拦截（逐单 error），台账仍 1 行。"""
    order_id = _seed_planned(eval_api)

    _confirm(eval_api.client, eval_api.headers(), [_inbound_item(order_id)])
    resp = _confirm(eval_api.client, eval_api.headers(), [_inbound_item(order_id)])

    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert result["status"] == JobStatus.VERIFIED.value
    assert result["error"] is not None, "重复确认应被状态机拦截"
    assert len(eval_api.ledgers()) == 1, "不得写出第二条台账"


def test_write_failure_no_batch_returns_to_planned_without_ledger(eval_api):
    """无生产批号 → 写台账失败回 `PLANNED`（正常分支，error=None），零台账。"""
    order_id = _seed_planned(eval_api, batch_no=None)

    resp = _confirm(eval_api.client, eval_api.headers(), [_inbound_item(order_id)])

    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert result["status"] == JobStatus.PLANNED.value
    assert result["error"] is None
    assert eval_api.ledgers() == ()


def test_optimistic_lock_stale_version_is_blocked(eval_api):
    """乐观锁：携带陈旧 `lock_version` → 状态机冲突（逐单 error），零台账、状态不动。"""
    order_id = _seed_planned(eval_api)

    resp = _confirm(eval_api.client, eval_api.headers(), [_inbound_item(order_id, lock_version=5)])

    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert result["status"] == JobStatus.PLANNED.value
    assert result["error"] is not None
    assert eval_api.ledgers() == ()


# ------------------------------------------------------------------ 顺路取（只读）


def _seed_outbound(eval_api, *, in_stock: bool):
    """一条出库单（料号 M1），`in_stock` 决定库里有没有该料的库存。"""
    scenario = eval_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, "B260801", 10)] if in_stock else [],
        job_orders=[
            JobOrderSpec(order_no="DO-01", material_code=MATERIAL, qty=5, job_type=JobType.OUTBOUND),
        ],
    )
    return str(scenario.job_orders[0].id)


def test_golden_008_pick_sequence_is_readonly(eval_api):
    """golden_008：顺路取只读派生 → 产出拣货方案、迁 `PLANNED`，零台账。"""
    order_id = _seed_outbound(eval_api, in_stock=True)

    resp = _pick(eval_api.client, eval_api.headers(), [order_id])

    assert resp.status_code == 200
    body = resp.json()
    assert body["plans"] != []
    assert body["not_in_stock"] == []
    assert eval_api.ledgers() == (), "顺路取只读派生，不写台账"


def test_golden_036_not_in_stock_is_listed_and_not_blocked(eval_api):
    """golden_036：货未入库 → 分列 `not_in_stock`、不阻断，单停留 `PENDING`。"""
    order_id = _seed_outbound(eval_api, in_stock=False)

    resp = _pick(eval_api.client, eval_api.headers(), [order_id])

    assert resp.status_code == 200
    body = resp.json()
    assert body["plans"] == []
    assert body["not_in_stock"] == [order_id]
    assert eval_api.orders()[0].status is JobStatus.PENDING


def test_golden_012_missing_snapshot_blocks_outbound(eval_api):
    """golden_012：无快照 → 出库顺路取 409 阻断（不猜测落位），零写入。"""
    scenario = eval_api.seed(
        job_orders=[
            JobOrderSpec(order_no="DO-01", material_code=MATERIAL, qty=5, job_type=JobType.OUTBOUND),
        ],
        snapshot_time=None,
    )
    order_id = str(scenario.job_orders[0].id)

    resp = _pick(eval_api.client, eval_api.headers(), [order_id])

    assert resp.status_code == 409
    assert resp.json()["error"] == "blocked_missing_prerequisite"
    assert eval_api.orders()[0].status is JobStatus.PENDING
