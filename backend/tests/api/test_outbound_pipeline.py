"""出库 7 步管线集成冒烟（tasks.md 5.1）。

事实来源：15-03 §2.3（出库 7 步管线）、§3.2（顺路取 = 按巷道聚合既有库存）、
          §6.2/§6.3（逐单处置与写台账）、§8.1（加权集中度 80% ≤ N）
          17-数据模型设计 §4.3（台账）、§4.4（后验）
          spec `transaction-base`「批量顺路取接口」「出库确认记录最终拣货路径」
          design.md D2 / D4 / D5

钉住的口径：把「出库单入队 → 队列可查 → 批量顺路取派生 → 逐单确认（带 `pick_path`）→
写台账 `pick_path_json`（出库无源无目标）→ 后验落「拣货量加权集中度」PASS」这一整条
出库回环跑通一遍。单测里不重复验派生细节（`test_pick_sequence.py` / `test_outbound.py`
已钉），这里只验「步接得上」：每一段把上一段的产物喂给下一段，最终落在台账 + 后验两处事实。

出库单入队用 `job_api.seed` 模拟 DO 导入的落点（`OUTBOUND` 单 + 库存快照），`batch_no`
**留空** —— 与真实 DO 导入一致（`importer/execute.py` 注释：出库单导入不留批号）。批号由
顺路取派生回写（FIFO 最早批），确认链写台账读的就是这个非空 `batch_no`，从而钉住
「导入留空 → 顺路取回写 → 确认写台账」这条真实链路（不绕过、不手摆批号）。

后验取 `PASS` 的现场：M1 全在巷道 01（40 板），顺路取派生单巷 → 集中度 1 ≤ 5 达标。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType, LedgerType, VerifyResult
from tests.api.conftest import Api
from tests.logic.conftest import (
    DEFAULT_WAREHOUSE_ID,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
)

pytestmark = pytest.mark.api

WAREHOUSE_ID = DEFAULT_WAREHOUSE_ID
MATERIAL = "M1"
BATCH = "B1"

JOBS_URL = "/api/jobs"
PICK_SEQUENCE_URL = "/api/job/batch/pick-sequence"
CONFIRM_URL = "/api/job/batch/confirm"


def test_outbound_pipeline_end_to_end(job_api: Api) -> None:
    """出库回环七步接得上：入队 → 队列 → 顺路取 → 逐单确认 → 台账 + 后验。

    库存分布（物料级 / 批号级）：巷道 01 仅 M1 40 板（批号 B1）。顺路取派生单巷方案
    `[{aisle: 01, qty: 40, batches: [B1]}]`；逐单确认接受该方案（带 `pick_path`），
    台账无源无目标、`pick_path_json` 记确认值；后验集中度 1 → PASS。
    """
    # 出库单已入队（DO 导入落点：OUTBOUND 单 + 库存快照）。
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, BATCH, 40)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=40,
                job_type=JobType.OUTBOUND,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    # 步 1：出库队列可查（`type=OUTBOUND` 大写，`JobType` 是大小写敏感的 str, Enum）。
    rows = job_api.client.get(
        JOBS_URL,
        params={"warehouse_id": WAREHOUSE_ID, "type": "OUTBOUND"},
        headers=job_api.headers,
    ).json()
    assert [r["job_order_id"] for r in rows] == [order_id]
    assert rows[0]["status"] == JobStatus.PENDING.value

    # 步 2：批量顺路取 → 派生单巷方案、迁 PLANNED（只读派生，不写台账）。
    pick_resp = job_api.client.post(
        PICK_SEQUENCE_URL,
        json={"warehouse_id": WAREHOUSE_ID, "job_order_ids": [order_id]},
        headers=job_api.headers,
    )
    assert pick_resp.status_code == 200
    (plan,) = pick_resp.json()["plans"]
    assert plan["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 40,
            "batches": [BATCH],
            "locations": [{"location_code": "010101", "qty": 40, "batch_no": BATCH}],
        }
    ]

    # 步 3：逐单确认（带 `pick_path`，接受顺路取方案）→ EXECUTED → 后验 → VERIFIED。
    confirm_resp = job_api.client.post(
        CONFIRM_URL,
        json={
            "warehouse_id": WAREHOUSE_ID,
            "orders": [{"job_order_id": order_id, "pick_path": plan["pick_sequence"]}],
        },
        headers=job_api.headers,
    )
    assert confirm_resp.status_code == 200
    (outcome,) = confirm_resp.json()["results"]
    assert outcome["status"] == JobStatus.VERIFIED.value

    # 步 4：台账（OUTBOUND 无源无目标，拣货路径记 `pick_path_json` 确认值；`batch_no` 是
    # 顺路取回写的 FIFO 最早批，非空可追溯）。
    (ledger,) = job_api.ledgers()
    assert ledger.ledger_type is LedgerType.OUTBOUND
    assert ledger.batch_no == BATCH
    assert ledger.source_location_code is None
    assert ledger.target_location_code is None
    assert ledger.pick_path_json == [
        {
            "aisle": "01",
            "qty": 40,
            "batches": [BATCH],
            "locations": [{"location_code": "010101", "qty": 40, "batch_no": BATCH}],
        }
    ]

    # 步 5：后验（拣货量加权集中度 = 1 → PASS）。
    (verification,) = job_api.verifications()
    assert verification.metric_kind == "拣货量加权集中度"
    assert verification.actual_value == 1.0
    assert verification.verify_result is VerifyResult.PASS

    assert job_api.orders()[0].status is JobStatus.VERIFIED
