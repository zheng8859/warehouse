"""单作业单动作端点（reject / retry / void）的契约测试（tasks.md 6.2 的验证）。

事实来源：design.md D6（端点清单）
          spec `transaction-base`「作业单写操作端点」（写操作二次确认语义）
          17-数据模型设计 §4.5（逐单处置 / 驳回原因）、§4.3（台账反向行）
          CLAUDE.md §四（未确认不产生台账）

## 三个端点各验一件事

| 端点 | 状态迁移 | 要钉住的口径 |
|---|---|---|
| reject | `PLANNED → REJECTED` | **未确认不产生台账**（红线） |
| retry  | `VERIFY_FAILED → VERIFIED` | 后验失败只有重试边（无放弃终态） |
| void   | `EXECUTED/VERIFIED → VOID` | 反向台账行 + 整单回冲（终态幂等） |

「未确认不产生台账」落在 reject 上最直白：一张 `PLANNED`（已出方案、未确认）的单被驳回，
只动状态与驳回原因，**没有任何台账 / cap 写入** —— 确认是写台账的唯一起点（`15` §6.3）。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType, LedgerType
from tests.api.conftest import Api
from tests.logic.conftest import JobOrderSpec

pytestmark = pytest.mark.api

MATERIAL = "M1"
BATCH = "B26090801"


def _seed_planned(job_api: Api, *, order_no: str, status=JobStatus.PLANNED, **overrides):
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no=order_no, material_code=MATERIAL, qty=40,
                batch_no=BATCH, job_type=JobType.INBOUND, status=status, **overrides,
            ),
        ],
    )
    return str(scenario.job_orders[0].id)


def _url(job_id: str, action: str) -> str:
    return f"/api/job/{job_id}/{action}"


# ------------------------------------------------------------------ reject

def test_reject_moves_planned_to_rejected_without_writing_a_ledger(job_api: Api) -> None:
    """`PLANNED → REJECTED` + 写 `reject_reason`；**未确认不产生台账**。"""
    order_id = _seed_planned(job_api, order_no="PO-01")

    response = job_api.client.post(
        _url(order_id, "reject"),
        json={"warehouse_id": "GTJ10036", "reason": "品项取消"},
        headers=job_api.headers,
    )

    assert response.status_code == 200
    assert response.json() == {"job_order_id": order_id, "status": JobStatus.REJECTED.value}

    (order,) = job_api.orders()
    assert order.status is JobStatus.REJECTED
    assert order.reject_reason == "品项取消"
    assert job_api.ledgers() == (), "驳回不产生台账 —— 红线「未确认不产生台账」"


def test_reject_of_a_non_planned_order_is_rejected(job_api: Api) -> None:
    """`PENDING` 驳回 ⇒ 409（`PENDING → REJECTED` 不是合法迁移）。"""
    order_id = _seed_planned(job_api, order_no="PO-01", status=JobStatus.PENDING)

    response = job_api.client.post(
        _url(order_id, "reject"),
        json={"warehouse_id": "GTJ10036"},
        headers=job_api.headers,
    )

    assert response.status_code == 409
    assert response.json()["error"] == "state_conflict"
    assert job_api.ledgers() == ()


# ------------------------------------------------------------------ retry

def test_retry_moves_verify_failed_to_verified(job_api: Api) -> None:
    """`VERIFY_FAILED → VERIFIED`：后验失败只有重试边，重试即再次走 `run_verification`。

    `VERIFY_FAILED` 直接造数（怎么走到这一步是逻辑层 `test_verify_flow` 的账）——
    这里验的是端点把「重试」接回后验编排，且落两行入库后验记录（同物料 / 同批）。
    """
    order_id = _seed_planned(
        job_api, order_no="PO-01", status=JobStatus.VERIFY_FAILED
    )

    response = job_api.client.post(
        _url(order_id, "retry"),
        json={"warehouse_id": "GTJ10036"},
        headers=job_api.headers,
    )

    assert response.status_code == 200
    assert response.json() == {"job_order_id": order_id, "status": JobStatus.VERIFIED.value}

    (order,) = job_api.orders()
    assert order.status is JobStatus.VERIFIED
    assert {v.metric_kind for v in job_api.verifications()} == {"同物料跨巷道", "同批跨巷道"}


def test_retry_of_a_verified_order_is_rejected(job_api: Api) -> None:
    """`VERIFIED` 重试 ⇒ 409（`VERIFIED → VERIFYING` 不是合法迁移；重试只开给失败态）。"""
    order_id = _seed_planned(job_api, order_no="PO-01", status=JobStatus.VERIFIED)

    response = job_api.client.post(
        _url(order_id, "retry"),
        json={"warehouse_id": "GTJ10036"},
        headers=job_api.headers,
    )

    assert response.status_code == 409
    assert response.json()["error"] == "state_conflict"


# ------------------------------------------------------------------ void

def test_void_reverses_and_writes_a_reverse_ledger_row(job_api: Api) -> None:
    """确认到 `VERIFIED` 后冲正：`VOID` 终态 + 一正常行 + 一反向行（`is_reversal`）。

    走完整链路（confirm → void）而不是直接造 `VERIFIED` 行：台账由确认写入，冲正取反的
    正是那行 —— 直接造数会把「反向行照抄正常行」这个事实藏掉。
    """
    order_id = _seed_planned(job_api, order_no="PO-01")

    confirmed = job_api.client.post(
        "/api/job/batch/confirm",
        json={
            "warehouse_id": "GTJ10036",
            "orders": [{"job_order_id": order_id, "target_location_code": "010104"}],
        },
        headers=job_api.headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["results"][0]["status"] == JobStatus.VERIFIED.value

    voided = job_api.client.post(
        _url(order_id, "void"),
        json={"warehouse_id": "GTJ10036"},
        headers=job_api.headers,
    )

    assert voided.status_code == 200
    assert voided.json() == {"job_order_id": order_id, "status": JobStatus.VOID.value}

    (order,) = job_api.orders()
    assert order.status is JobStatus.VOID

    ledgers = job_api.ledgers()
    assert [lg.is_reversal for lg in ledgers] == [False, True]
    normal, reverse = ledgers
    assert normal.ledger_type is LedgerType.INBOUND
    assert reverse.ledger_type is LedgerType.INBOUND
    assert reverse.target_location_code == "010104"


def test_void_is_idempotently_rejected_on_a_second_call(job_api: Api) -> None:
    """已 `VOID` 再冲正 ⇒ 409（终态幂等），仍只有一正常行 + 一反向行。"""
    order_id = _seed_planned(job_api, order_no="PO-01")
    job_api.client.post(
        "/api/job/batch/confirm",
        json={
            "warehouse_id": "GTJ10036",
            "orders": [{"job_order_id": order_id, "target_location_code": "010104"}],
        },
        headers=job_api.headers,
    )
    first = job_api.client.post(
        _url(order_id, "void"), json={"warehouse_id": "GTJ10036"}, headers=job_api.headers
    )
    assert first.status_code == 200

    second = job_api.client.post(
        _url(order_id, "void"), json={"warehouse_id": "GTJ10036"}, headers=job_api.headers
    )

    assert second.status_code == 409
    assert second.json()["error"] == "state_conflict"
    assert [lg.is_reversal for lg in job_api.ledgers()] == [False, True]
