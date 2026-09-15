"""批量收拢方案端点 `POST /api/job/batch/relocate-plan` 的契约测试（tasks.md 2.3）。

事实来源：17 §10.3（收拢方案形状）、§4.1（作业单标识）、§4.2（方案实体）
          15-04 §4.2（三重校验：cap 充足 / 批号不变 / 集中度下降）、§4.3（降级链）
          design.md D2（纯函数派生）、D3（端点契约）、D4（幂等键 = bulk_batch_no × 库存视图版本）
          spec `transaction-base`「批量收拢方案接口」

复用 `tests/api/conftest.py` 的 `job_api` 夹具与 `tests/logic/conftest.py` 的
`make_scenario` 造数。仓管员有 `relocate.operate`（`13` §2.2），凭据默认可用。

钉住的口径（D2 / D3 / D4）：

  1. **只读派生**：`PENDING` 移库单按批号聚合散落板 → 定主巷道 → 三重校验 → 写
     `plan_kind=CONSOLIDATE` 方案、迁 `PLANNED`、回写 `bulk_batch_no`（不写台账 / 库存）。
  2. **降级链**：主巷道 cap 不足 → 次选巷道（方案记 `degrade_reason`）→ 仍不足 → 移出批量。
  3. **移出批量**：不可行的单记 `moved_out[]`（含降级原因）、停留 `PENDING`、不写方案。
  4. **幂等命中**：已 `PLANNED` 且当前方案 `snapshot_version` 相同 → 返既有方案、不重复写、
     不重迁状态。
  5. **无快照 409**：阻断、零写入。

  造数用 `abc_class="A"`（`available_cap` 对 A 类恒返 `cap_total`，与释放钟点无关）——
  这样用例断言 `available` 不受现场墙上时间影响（否则 `cap_usable` 与「是否过了 18:00
  释放钟点」绑定，用例在白天/晚上跑结果不同，违反「同样输入必得同样输出」）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import JobStatus, JobType
from app.models.job import JobOrder, PlanKind, RecommendationPlan
from tests.logic.conftest import (
    DEFAULT_SNAPSHOT_TIME,
    DEFAULT_WAREHOUSE_ID,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
)

pytestmark = pytest.mark.api

RELOCATE_PLAN_URL = "/api/job/batch/relocate-plan"
WAREHOUSE_ID = DEFAULT_WAREHOUSE_ID
SNAPSHOT_VERSION = DEFAULT_SNAPSHOT_TIME.strftime("%Y-%m-%dT%H:%M")

MATERIAL = "M1"
BATCH = "B1"


def _relocate(job_api, job_order_ids: list[str]):
    return job_api.client.post(
        RELOCATE_PLAN_URL,
        json={"warehouse_id": WAREHOUSE_ID, "job_order_ids": job_order_ids},
        headers=job_api.headers,
    )


def _plan_rows(job_api) -> tuple[RecommendationPlan, ...]:
    with job_api.factory() as session:
        return tuple(
            session.scalars(select(RecommendationPlan).order_by(RecommendationPlan.id))
        )


def _order(job_api) -> JobOrder:
    return job_api.orders()[0]


def _scatter_scenario(job_api, *, caps: dict[str, int]) -> tuple[str, ...]:
    """一条移库单（料号 M1、批号 B1）的散落场景。

    库存分布（物料级 / 批号级同示）：
      - 巷道 01：M1 30 板（其中 B1 5 板、B2 25 板）
      - 巷道 02：M1 20 板（全部 B2，无 B1）
      - 巷道 03：M1 10 板（全部 B1）

    故 `profile.plates_by_aisle = {01:30, 02:20, 03:10}` → 主巷道 01、次选 02；
    批号 B1 = `{01:5, 03:10}`。`caps` 给各巷道的 `cap_total`（A 类 available = cap_total）。
    """
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, abc_class="A", material_name="茉莉柚茶")],
        aisles=[
            AisleSpec(aisle_no, cap_total=cap) for aisle_no, cap in sorted(caps.items())
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
                qty=20,
                job_type=JobType.RELOCATE,
                batch_no=BATCH,
                abc_class="A",
            )
        ],
    )
    return tuple(str(o.id) for o in scenario.job_orders)


# ------------------------------------------------------------------ ① 派生迁 PLANNED

def test_pending_relocate_is_derived_and_marked_planned(job_api) -> None:
    """`PENDING` 移库单派生 → 写 `plan_kind=CONSOLIDATE` 方案、迁 `PLANNED`、回写批次号。

    主巷道 = 01（物料板数最多），批号 B1 散落在 03（10 板，独占）→ `from_aisles=[03]`、
    `plates=10`；收拢后 03 变空 → `after = 3 − 1 = 2`。钉住「不写台账」—— 未确认不产生
    台账（`CLAUDE.md` §四）。
    """
    (order_id,) = _scatter_scenario(job_api, caps={"01": 100, "02": 100, "03": 100})

    body = _relocate(job_api, [order_id]).json()

    assert body["snapshot_version"] == SNAPSHOT_VERSION
    assert body["moved_out"] == []
    (plan,) = body["plans"]
    assert plan["job_order_id"] == order_id
    assert plan["material_code"] == MATERIAL
    assert plan["batch_no"] == BATCH
    assert plan["target_aisle"] == "01"
    assert plan["from_aisles"] == ["03"]
    assert plan["plates"] == 10
    assert plan["expected_cross_aisle"] == {"before": 3, "after": 2}
    assert plan["batch_unchanged"] is True
    assert plan["degrade_reason"] is None
    # 缺口 2：真实库位 —— 源库位取自库存行（030101），目标库位 = 目标巷道内该物料
    # 既有库位的最低库位号（010101），不是「巷道 + 固定后缀」的编造。
    assert plan["source_locations"] == [{"location_code": "030101", "qty": 10}]
    assert plan["target_location"] == "010101"
    assert isinstance(plan["plan_id"], int)

    order = _order(job_api)
    assert order.status is JobStatus.PLANNED
    assert order.bulk_batch_no == body["bulk_batch_no"]
    assert order.lock_version == 1

    (row,) = _plan_rows(job_api)
    assert row.plan_kind is PlanKind.CONSOLIDATE
    assert row.job_order_id == int(order_id)
    assert row.payload_json["snapshot_version"] == SNAPSHOT_VERSION

    assert job_api.ledgers() == (), "收拢方案是只读派生，未确认不得产生台账"


# ------------------------------------------------------------------ ② 主巷道 cap 不足降级次选

def test_cap_insufficient_main_degrades_to_second(job_api) -> None:
    """主巷道 cap 不足 → 降级次选巷道（板数第二多），方案记 `degrade_reason`（降级不静默）。

    主巷道 01 可用 5 格 < 收拢 03 的 10 板 → 降级次选 02（可用 100）。次选的 `from_aisles`
    = 非 02 的批号巷道 `[01, 03]`、`plates = 5 + 10 = 15`；收拢后 03 变空、01 仍与 B2 共占
    → `after = 3 − 1 = 2`。
    """
    (order_id,) = _scatter_scenario(job_api, caps={"01": 5, "02": 100, "03": 100})

    body = _relocate(job_api, [order_id]).json()

    (plan,) = body["plans"]
    assert plan["target_aisle"] == "02"
    assert plan["from_aisles"] == ["01", "03"]
    assert plan["plates"] == 15
    assert plan["expected_cross_aisle"] == {"before": 3, "after": 2}
    assert plan["degrade_reason"] and "容量不足" in plan["degrade_reason"]
    # 次选巷道的逐格源库位覆盖两个散落巷道（01 的 010101 5 箱 + 03 的 030101 10 箱），
    # 目标库位 = 次选巷道 02 内该物料的既有库位（020101）。
    assert plan["source_locations"] == [
        {"location_code": "010101", "qty": 5},
        {"location_code": "030101", "qty": 10},
    ]
    assert plan["target_location"] == "020101"

    order = _order(job_api)
    assert order.status is JobStatus.PLANNED
    assert _plan_rows(job_api)[0].plan_kind is PlanKind.CONSOLIDATE


# ------------------------------------------------------------------ ③ 仍不足移出批量

def test_cap_insufficient_both_moves_out(job_api) -> None:
    """主巷道与次选容量均不足 → 移出批量（`moved_out[]` + 降级原因），单停留 `PENDING`。

    `moved_out[]` 与 `plans[]` **互斥**：不可行的单不出方案、不写方案行、不领批次号
    （「降级不静默」—— 原因里写明主/次选各自为何不足）。
    """
    (order_id,) = _scatter_scenario(job_api, caps={"01": 5, "02": 5, "03": 100})

    body = _relocate(job_api, [order_id]).json()

    assert body["plans"] == []
    (moved,) = body["moved_out"]
    assert moved["job_order_id"] == order_id
    assert "容量" in moved["reason"]

    order = _order(job_api)
    assert order.status is JobStatus.PENDING
    assert order.bulk_batch_no is None
    assert _plan_rows(job_api) == ()


# ------------------------------------------------------------------ ④ 幂等命中

def test_idempotent_resubmit_returns_existing_plan_without_rewriting(job_api) -> None:
    """已 `PLANNED` 且视图未变 → 返既有方案（同 `plan_id`）、不重复写、不重迁状态。

    幂等键 = `bulk_batch_no` × 库存视图版本（D4）。第二次提交：`plans` 返同一条 `plan_id`、
    方案行仍是一条、`lock_version` 不再推进（未发生 `PENDING → PLANNED` 迁移）。
    """
    (order_id,) = _scatter_scenario(job_api, caps={"01": 100, "02": 100, "03": 100})

    first = _relocate(job_api, [order_id]).json()
    (first_plan,) = first["plans"]
    assert _order(job_api).lock_version == 1

    second = _relocate(job_api, [order_id]).json()

    (second_plan,) = second["plans"]
    assert second_plan["plan_id"] == first_plan["plan_id"], "幂等命中应返既有方案，而非追加新行"
    assert second_plan["target_aisle"] == "01"
    assert _order(job_api).status is JobStatus.PLANNED
    assert _order(job_api).lock_version == 1, "幂等命中不重迁状态、不推进乐观锁"
    assert len(_plan_rows(job_api)) == 1


# ------------------------------------------------------------------ ⑤ 旧格式方案回填不 500

def test_legacy_plan_without_source_locations_is_rederived(job_api) -> None:
    """缺口 2 之前的旧方案缺 `source_locations` / `target_location` → 不 500，重派生追加新行。

    `RelocatePlanItem` 现在必填 `source_locations`（`min_length=1`）与 `target_location`
    （`min_length=6`）；历史库里缺口 2 之前的方案没有这两项，幂等分支原样回填会撞
    Pydantic 必填校验（500）。回归钉住：幂等命中要同时校验「版本相同 **且** 格式当前」，
    旧格式视作「格式过期」走重派生，追加一行当前格式的新方案。
    """
    (order_id,) = _scatter_scenario(job_api, caps={"01": 100, "02": 100, "03": 100})

    first = _relocate(job_api, [order_id]).json()
    (first_plan,) = first["plans"]
    assert first_plan["plan_id"] is not None

    # 把已落库的方案改造成缺口 2 之前的旧格式（去掉逐格源库位 / 目标库位）。
    # `payload_json` 是 sa.JSON（非 MutableDict），原地 pop 不被追踪 —— 整体重赋值。
    with job_api.factory() as session:
        row = session.scalars(select(RecommendationPlan)).one()
        payload = dict(row.payload_json)
        payload.pop("source_locations", None)
        payload.pop("target_location", None)
        row.payload_json = payload
        session.commit()

    second = _relocate(job_api, [order_id]).json()

    (second_plan,) = second["plans"]
    assert second_plan["source_locations"] == [{"location_code": "030101", "qty": 10}]
    assert second_plan["target_location"] == "010101"
    assert second_plan["plan_id"] != first_plan["plan_id"], "旧格式应重派生、追加新方案行，而非回填旧行"
    assert _order(job_api).status is JobStatus.PLANNED
    assert len(_plan_rows(job_api)) == 2


# ------------------------------------------------------------------ ⑥ 无快照 409

def test_missing_snapshot_blocks_the_batch(job_api) -> None:
    """无快照 → 409 阻断、零写入（D3：提示重新导入，不猜测）。

    收拢方案按库存视图聚合，无快照即无分布可读 —— 与入库分配的「无快照降级不阻断」不同，
    移库侧是阻断（`CLAUDE.md` §四「快照缺失或过期 → 阻断并提示重新导入」）。
    """
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="MV-20260908-001",
                material_code=MATERIAL,
                qty=20,
                job_type=JobType.RELOCATE,
                batch_no=BATCH,
            )
        ],
        snapshot_time=None,
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _relocate(job_api, [order_id])

    assert response.status_code == 409
    assert response.json()["error"] == "blocked_missing_prerequisite"
    assert _plan_rows(job_api) == ()
    assert _order(job_api).status is JobStatus.PENDING
