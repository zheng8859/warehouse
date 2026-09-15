"""移库 7 步管线集成冒烟（tasks.md 3.1）。

事实来源：15-入库出库移库与后验流程设计 §2.3（移库 7 步管线）、§6.2/§6.3（逐单处置与
          写台账）、§7（后验与偏离）
          15-04 §8.1（移库后验 = 相对阈值：收拢后同物料跨巷道数低于收拢前）
          17-数据模型设计 §4.3（台账）、§4.4（后验 / 偏离）
          spec `transaction-base`「批量收拢方案接口」「移库台账与后验」
          design.md D5（六模块 + 单事务）

钉住的口径：把「偏离批次是移库任务来源 → 队列可查 → 批量派生收拢方案 → 逐单确认 →
写移库台账（源 / 目标都写、批号不变）→ 后验刷新（`同物料跨巷道是否下降` PASS）」这一整
条治理回环跑通一遍。**单测里不重复验派生细节**（`test_relocate_plan.py` 已钉），这里只验
「七步接得上」：每一段把上一段的产物喂给下一段，最终落在台账 + 后验两处事实。

后验取 `PASS` 的现场：移库前 M1 散在 3 巷（01 / 02 / 03），批号 B1 独占 03（10 板）；
确认把 03 的 10 板移入 01 后，03 变空 → 跨巷道 3 → 2，`2 < 3` 达标。
"""
from __future__ import annotations

import pytest

from app.core.enums import JobStatus, JobType, LedgerType, VerifyResult
from app.models.job import Deviation, DeviationCauseKind, DeviationStatus
from tests.api.conftest import Api
from tests.logic.conftest import (
    DEFAULT_WAREHOUSE_ID,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
)

pytestmark = pytest.mark.api

WAREHOUSE_ID = DEFAULT_WAREHOUSE_ID
MATERIAL = "M1"
BATCH = "B1"

RELOCATE_PLAN_URL = "/api/job/batch/relocate-plan"
CONFIRM_URL = "/api/job/batch/confirm"
DEVIATION_URL = "/api/deviation"
JOBS_URL = "/api/jobs"


def _seed_deviation(job_api: Api) -> None:
    """造一条偏离批次（`make_scenario` 无 `DeviationSpec`，这里走模型直写）。

    偏离是移库作业的任务来源（15 §7.2）；本用例里它与移库单按「同一物料 / 批号」对应，
    不建 `relocate_job_order_id` 关联（`OPEN` 状态不要求，见 `_DEVIATION_RELOCATE_CHECK`）。
    """
    with job_api.factory() as session:
        session.add(
            Deviation(
                warehouse_id=WAREHOUSE_ID,
                material_code=MATERIAL,
                batch_no=BATCH,
                actual_cross_aisle=6,
                threshold_cross_aisle=5,
                cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
                status=DeviationStatus.OPEN,
            )
        )
        session.commit()


def test_relocate_pipeline_end_to_end(job_api: Api) -> None:
    """移库治理回环七步接得上：偏离 → 队列 → 收拢方案 → 逐单确认 → 台账 + 后验。

    库存分布（物料级 / 批号级）：
      - 巷道 01：M1 30 板（B1 5、B2 25）
      - 巷道 02：M1 20 板（全 B2）
      - 巷道 03：M1 10 板（全 B1）
    故主巷道 = 01、批号 B1 = `{01:5, 03:10}`，收拢方案 = 把 03 的 10 板移入 01。
    """
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, abc_class="A", material_name="茉莉柚茶")],
        aisles=[
            AisleSpec("01", cap_total=100),
            AisleSpec("02", cap_total=100),
            AisleSpec("03", cap_total=100),
        ],
        inventory=[
            InventorySpec("010101", MATERIAL, "B1", 5),
            InventorySpec("010102", MATERIAL, "B2", 25),
            InventorySpec("020101", MATERIAL, "B2", 20),
            InventorySpec("030101", MATERIAL, "B1", 10),
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-20260908-001",
                material_code=MATERIAL,
                qty=10,
                job_type=JobType.RELOCATE,
                batch_no=BATCH,
                abc_class="A",
            )
        ],
    )
    (order_id,) = (str(o.id) for o in scenario.job_orders)

    # 步 1：偏离清单可查（移库任务来源）。
    _seed_deviation(job_api)
    deviations = job_api.client.get(
        DEVIATION_URL, params={"warehouse_id": WAREHOUSE_ID}, headers=job_api.headers
    ).json()
    assert [d["material_code"] for d in deviations] == [MATERIAL]
    assert deviations[0]["batch_no"] == BATCH
    assert deviations[0]["status"] == DeviationStatus.OPEN.value

    # 步 2：移库队列可查（type=RELOCATE 大写，`JobType` 是大小写敏感的 str, Enum）。
    rows = job_api.client.get(
        JOBS_URL,
        params={"warehouse_id": WAREHOUSE_ID, "type": "RELOCATE"},
        headers=job_api.headers,
    ).json()
    assert [r["job_order_id"] for r in rows] == [order_id]
    assert rows[0]["status"] == JobStatus.PENDING.value

    # 步 3：批量收拢方案 → 主巷道 01、from 03、迁 PLANNED。
    plan_resp = job_api.client.post(
        RELOCATE_PLAN_URL,
        json={"warehouse_id": WAREHOUSE_ID, "job_order_ids": [order_id]},
        headers=job_api.headers,
    )
    assert plan_resp.status_code == 200
    (plan,) = plan_resp.json()["plans"]
    assert plan["target_aisle"] == "01"
    assert plan["from_aisles"] == ["03"]
    assert plan["plates"] == 10

    # 步 4：逐单确认（移库逐格源库位 + 目标库位）→ EXECUTED → 后验 → VERIFIED。
    #       源库位取自方案的 `source_locations`（真实库位号，非编造「巷道 + 固定后缀」）。
    confirm_resp = job_api.client.post(
        CONFIRM_URL,
        json={
            "warehouse_id": WAREHOUSE_ID,
            "orders": [
                {
                    "job_order_id": order_id,
                    "source_locations": [{"location_code": "030101", "qty": 10}],
                    "target_location_code": "010101",
                }
            ],
        },
        headers=job_api.headers,
    )
    assert confirm_resp.status_code == 200
    (outcome,) = confirm_resp.json()["results"]
    assert outcome["status"] == JobStatus.VERIFIED.value

    # 步 5：台账（RELOCATE，源 / 目标都写、批号不变）+ 后验（同物料跨巷道是否下降 PASS）。
    (ledger,) = job_api.ledgers()
    assert ledger.ledger_type is LedgerType.RELOCATE
    assert ledger.source_location_code == "030101"
    assert ledger.target_location_code == "010101"
    assert ledger.batch_no == BATCH, "移库不改批号"
    assert ledger.qty == 10

    (verification,) = job_api.verifications()
    assert verification.metric_kind == "同物料跨巷道是否下降"
    assert verification.actual_value == 2.0
    assert verification.threshold_value == 3.0
    assert verification.verify_result is VerifyResult.PASS

    assert job_api.orders()[0].status is JobStatus.VERIFIED
