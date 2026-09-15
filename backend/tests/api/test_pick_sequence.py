"""批量顺路取端点 `POST /api/job/batch/pick-sequence` 的契约测试（tasks.md 2.3）。

事实来源：17 §10.2（顺路取顺序形状）、§4.1（作业单标识）、§4.2（方案实体）
          15-03 §3.2（顺路取 = 按巷道聚合既有库存）、§6.2（超标高亮不阻断）
          design.md D2（端点契约）、D3（幂等键 = bulk_batch_no × 库存视图版本）
          spec `transaction-base`「批量顺路取接口」

复用 `tests/api/conftest.py` 的 `job_api` 夹具（独立库 + 覆盖应用 + 仓管员凭据，见那个
文件的模块 docstring）与 `tests/logic/conftest.py` 的 `make_scenario` 造数。仓管员有
`outbound.operate`（`13` §2.2），故凭据默认可用。

钉住的口径（D2 / D3）：

  1. **只读派生**：`PENDING` 单按 `SnapshotIndex.profile` 派生 → 写 `plan_kind=PICK` 方案、
     迁 `PLANNED`、回写 `bulk_batch_no`（不写台账、不写库存）。
  2. **货未入库分列 `not_in_stock`**：`profile.plates_by_aisle` 空 → 分列提示、不阻断，
     单停留 `PENDING`。
  3. **幂等命中**：已 `PLANNED` 且当前方案 `snapshot_version` 相同 → 返既有方案、不重复写、
     不重迁状态。
  4. **视图推进**：`snapshot_version` 不同 → 重新派生、**追加**一行新方案（`id` 更大）。
  5. **无快照 409**：阻断、零写入。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.enums import JobStatus, JobType
from app.models.job import JobOrder, PlanKind, RecommendationPlan
from tests.logic.conftest import (
    DEFAULT_SNAPSHOT_TIME,
    DEFAULT_WAREHOUSE_ID,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
)

PICK_SEQUENCE_URL = "/api/job/batch/pick-sequence"
WAREHOUSE_ID = DEFAULT_WAREHOUSE_ID
SNAPSHOT_VERSION = DEFAULT_SNAPSHOT_TIME.strftime("%Y-%m-%dT%H:%M")

MATERIAL = "MOK"


def _pick_sequence(job_api,job_order_ids: list[str]) -> "object":
    return job_api.client.post(
        PICK_SEQUENCE_URL,
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


def _outbound_scenario(job_api,*, in_stock: bool = True):
    """一条出库单（料号 MOK），`in_stock` 决定库里有没有该料的库存。"""
    return job_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, "B260801", 10)] if in_stock else [],
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                job_type=JobType.OUTBOUND,
            )
        ],
    )


# ------------------------------------------------------------------ ① 派生迁 PLANNED

def test_pending_order_is_derived_and_marked_planned(job_api) -> None:
    """PENDING 出库单派生 → 写 `plan_kind=PICK` 方案、迁 `PLANNED`、回写批次号 + FIFO 批号。

    顺路取是**只读派生**：`pick_sequence` 按订单量 5 封顶（库存 10 板 → 拣 5），加权集中度
    = 1（一条巷道全覆盖），不超 N=5；`order.batch_no` 回写 FIFO 最早批 `B260801`。同时钉住
    「不写台账」—— 未确认不产生台账（`CLAUDE.md` §四）。
    """
    scenario = _outbound_scenario(job_api)
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    body = _pick_sequence(job_api,[order_id]).json()

    assert body["snapshot_version"] == SNAPSHOT_VERSION
    (plan,) = body["plans"]
    assert plan["job_order_id"] == order_id
    assert plan["do_no"] == "DO-20260908-001"
    assert plan["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 5,
            "batches": ["B260801"],
            "locations": [{"location_code": "010101", "qty": 5, "batch_no": "B260801"}],
        }
    ]
    assert plan["weighted_concentration"] == 1
    assert plan["threshold_n"] == 5
    assert plan["exceeded"] is False
    assert isinstance(plan["plan_id"], int)

    order = _order(job_api)
    assert order.status is JobStatus.PLANNED
    assert order.batch_no == "B260801"  # 顺路取回写 FIFO 最早批（出库单导入时留空）
    assert order.bulk_batch_no == body["bulk_batch_no"]
    assert order.lock_version == 1

    (row,) = _plan_rows(job_api)
    assert row.plan_kind is PlanKind.PICK
    assert row.job_order_id == int(order_id)
    assert row.payload_json["snapshot_version"] == SNAPSHOT_VERSION

    assert job_api.ledgers() == (), "顺路取是只读派生，未确认不得产生台账"


# ------------------------------------------------------------------ ② 货未入库分列 not_in_stock

def test_out_of_stock_order_is_listed_in_not_in_stock_and_not_blocked(job_api) -> None:
    """货未入库（`profile.plates_by_aisle` 空）→ 分列 `not_in_stock`、不阻断，单停留 PENDING。

    不阻断 = 同批其余单照常派生；这里只有一张单，故 `plans` 空、`not_in_stock` 单列它、
    零方案行、状态不动。D2 原文：提示「该品项尚未入库」，不猜测落位。
    """
    scenario = _outbound_scenario(job_api,in_stock=False)
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _pick_sequence(job_api,[order_id])

    assert response.status_code == 200
    body = response.json()
    assert body["plans"] == []
    assert body["not_in_stock"] == [order_id]

    order = _order(job_api)
    assert order.status is JobStatus.PENDING
    assert order.bulk_batch_no is None
    assert _plan_rows(job_api) == ()


# ------------------------------------------------------------------ ③ 幂等命中

def test_idempotent_resubmit_returns_existing_plan_without_rewriting(job_api) -> None:
    """已 PLANNED 且视图未变 → 返既有方案（同 `plan_id`）、不重复写、不重迁状态。

    幂等键 = `bulk_batch_no` × 库存视图版本（D3）。第二次提交：`plans` 返回同一条 `plan_id`、
    方案行仍是一条、`lock_version` 不再推进（未发生 `PENDING → PLANNED` 迁移）。
    """
    scenario = _outbound_scenario(job_api)
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    first = _pick_sequence(job_api,[order_id]).json()
    (first_plan,) = first["plans"]
    assert _order(job_api).lock_version == 1

    second = _pick_sequence(job_api,[order_id]).json()

    (second_plan,) = second["plans"]
    assert second_plan["plan_id"] == first_plan["plan_id"], "幂等命中应返既有方案，而非追加新行"
    assert second_plan["do_no"] == "DO-20260908-001"
    assert _order(job_api).status is JobStatus.PLANNED
    assert _order(job_api).lock_version == 1, "幂等命中不重迁状态、不推进乐观锁"
    assert len(_plan_rows(job_api)) == 1


# ------------------------------------------------------------------ ③½ 旧格式（无 locations）重派生升级

def test_old_format_plan_without_locations_re_derives_and_appends(job_api) -> None:
    """缺口 3 之前落库的旧方案（`pick_sequence` 无 `locations`）重提交 → 视为格式过期，
    重新派生并**追加**一行当前格式（含 `locations`）的新方案，而非原样回填旧形状。

    镜像 relocate 缺口 2 的 `source_locations` 守卫：旧形状不回填，重派生升级。
    """
    scenario = _outbound_scenario(job_api)
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    first = _pick_sequence(job_api, [order_id]).json()
    (first_plan,) = first["plans"]
    assert first_plan["pick_sequence"][0]["locations"], "新方案本应含库位级 locations"

    # 把当前方案改造成「缺口 3 之前」的旧格式（剥掉每巷的 locations 键），模拟历史落库行。
    with job_api.factory() as session:
        row = session.get(RecommendationPlan, first_plan["plan_id"])
        row.payload_json = {
            **row.payload_json,
            "pick_sequence": [
                {k: v for k, v in entry.items() if k != "locations"}
                for entry in row.payload_json["pick_sequence"]
            ],
        }
        session.commit()

    second = _pick_sequence(job_api, [order_id]).json()

    (second_plan,) = second["plans"]
    assert second_plan["plan_id"] > first_plan["plan_id"], "旧格式应重派生追加新行，而非返既有"
    assert second_plan["pick_sequence"][0]["locations"], "重派生后的方案应升级为含 locations"
    assert len(_plan_rows(job_api)) == 2, "旧方案保留（可追溯），新方案追加"


# ------------------------------------------------------------------ ④ 视图推进重新派生

def test_advanced_snapshot_version_re_derives_and_appends_a_plan(job_api) -> None:
    """视图推进（`snapshot_version` 变）→ 重新派生并**追加**一行新方案，不重迁状态。

    造法：先派生（快照 v1，库位 `010101` 巷道 01）；再导入一版新快照（v2，库位 `020101`
    巷道 02），重新提交同一单 → 追加 `id` 更大的新方案、响应返新 `plan_id`、`snapshot_version`
    为新版、`pick_sequence` 反映新分布。旧方案**保留**（`RecommendationPlan` 不唯一，
    历史可追溯）。
    """
    scenario = _outbound_scenario(job_api)
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    first = _pick_sequence(job_api,[order_id]).json()
    (first_plan,) = first["plans"]
    assert len(_plan_rows(job_api)) == 1

    # 推进视图：新快照 v2（时点 +1 天 ⇒ 新 `snapshot_version`），同料号落在巷道 02。
    job_api.seed(
        materials=(),
        inventory=[InventorySpec("020101", MATERIAL, "B260801", 8)],
        weights=None,
        capacity=None,
        snapshot_time=DEFAULT_SNAPSHOT_TIME + timedelta(days=1),
        snapshot_version_no=2,
    )
    new_version = (DEFAULT_SNAPSHOT_TIME + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")

    second = _pick_sequence(job_api,[order_id]).json()

    assert second["snapshot_version"] == new_version
    (second_plan,) = second["plans"]
    assert second_plan["job_order_id"] == order_id
    assert second_plan["plan_id"] > first_plan["plan_id"], "应追加新方案行，而非改写既有行"
    assert second_plan["pick_sequence"][0]["aisle"] == "02", "重派生应读新视图的分布"
    assert second_plan["snapshot_version"] == new_version

    rows = _plan_rows(job_api)
    assert len(rows) == 2, "旧方案保留（可追溯），新方案追加"
    assert [r.plan_kind for r in rows] == [PlanKind.PICK, PlanKind.PICK]
    assert rows[-1].id == second_plan["plan_id"]

    order = _order(job_api)
    assert order.status is JobStatus.PLANNED, "已 PLANNED，重派生不重迁状态"
    assert order.batch_no == "B260801", "重派生回写 FIFO 最早批（本场景两版视图同批）"
    assert order.lock_version == 2, "重派生回写 batch_no，推进乐观锁"


# ------------------------------------------------------------------ ⑤ 无快照 409

def test_missing_snapshot_blocks_the_batch(job_api) -> None:
    """无快照 → 409 阻断、零写入（D2：提示重新导入，不猜测落位）。

    顺路取按库存视图聚合，无快照即无分布可读 —— 与入库分配的「无快照降级不阻断」不同，
    出库侧是阻断（`CLAUDE.md` §四「快照缺失或过期（出库）→ 阻断」）。
    """
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                job_type=JobType.OUTBOUND,
            )
        ],
        snapshot_time=None,
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _pick_sequence(job_api,[order_id])

    assert response.status_code == 409
    assert response.json()["error"] == "blocked_missing_prerequisite"
    assert _plan_rows(job_api) == ()
    assert _order(job_api).status is JobStatus.PENDING


# ------------------------------------------------------------------ 报文级拒绝（D2 第 1 条）

def test_a_non_outbound_order_rejects_the_whole_batch(job_api) -> None:
    """混进一张非出库单 ⇒ 整批 422（D2：顺路取只处理 OUTBOUND）。"""
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="PO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                job_type=JobType.INBOUND,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _pick_sequence(job_api,[order_id])

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert _plan_rows(job_api) == ()


def test_a_non_derivable_status_rejects_the_whole_batch(job_api) -> None:
    """`EXECUTED` 单 ⇒ 整批 409（D3：只派生 `PENDING` / `PLANNED`）。"""
    scenario = job_api.seed(
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                job_type=JobType.OUTBOUND,
                status=JobStatus.EXECUTED,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _pick_sequence(job_api,[order_id])

    assert response.status_code == 409
    assert response.json()["error"] == "state_conflict"
    assert _plan_rows(job_api) == ()


# ------------------------------------------------------------------ 出库确认记录最终拣货路径（tasks.md 3.5）

CONFIRM_URL = "/api/job/batch/confirm"


def _confirm(job_api, orders: list[dict]) -> "object":
    return job_api.client.post(
        CONFIRM_URL,
        json={"warehouse_id": WAREHOUSE_ID, "orders": orders},
        headers=job_api.headers,
    )


def test_confirm_with_pick_path_writes_it_to_ledger(job_api) -> None:
    """出库确认带 `pick_path` → 台账 `pick_path_json` 写确认值、`source_location_code` NULL。

    D4：操作员微调后的最终拣货路径入账（决策权在人）。台账矩阵出库无源无目标，拣货分布
    记 `pick_path_json`（巷道序），源库位不再落单一库位。
    """
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, "B260801", 10)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                batch_no="B260801",
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _confirm(job_api, [
        {"job_order_id": order_id,
         "pick_path": [{"aisle": "01", "qty": 5, "batches": ["B260801"]}]},
    ])

    assert response.status_code == 200
    (result,) = response.json()["results"]
    assert result["status"] == JobStatus.VERIFIED.value

    (ledger,) = job_api.ledgers()
    assert ledger.source_location_code is None
    assert ledger.target_location_code is None
    assert ledger.pick_path_json == [
        {"aisle": "01", "qty": 5, "batches": ["B260801"], "locations": []}
    ]


def test_confirm_without_pick_path_falls_back_to_current_plan(job_api) -> None:
    """省略 `pick_path` → 回退读该单当前方案的 `pick_sequence`（未微调 = 接受推荐）。

    先跑批量顺路取派生方案（迁 `PLANNED` + 写当前方案），再确认时不带 `pick_path` ——
    编排读当前方案 `payload_json["pick_sequence"]` 落 `pick_path_json`。
    """
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, "B260801", 5)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                batch_no="B260801",
                job_type=JobType.OUTBOUND,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    pick = _pick_sequence(job_api, [order_id]).json()
    assert _order(job_api).status is JobStatus.PLANNED

    response = _confirm(job_api, [{"job_order_id": order_id}])

    assert response.status_code == 200
    (result,) = response.json()["results"]
    assert result["status"] == JobStatus.VERIFIED.value

    (ledger,) = job_api.ledgers()
    assert ledger.source_location_code is None
    assert ledger.pick_path_json == pick["plans"][0]["pick_sequence"]


def test_confirm_without_pick_path_and_no_plan_rejects(job_api) -> None:
    """既无 `pick_path` 也无当前方案 → 422（D4：不猜测拣货路径），零台账。"""
    scenario = job_api.seed(
        materials=[MaterialSpec(MATERIAL, material_name="茉莉柚茶")],
        inventory=[InventorySpec("010101", MATERIAL, "B260801", 5)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-20260908-001",
                material_code=MATERIAL,
                qty=5,
                batch_no="B260801",
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    (order_id,) = [str(o.id) for o in scenario.job_orders]

    response = _confirm(job_api, [{"job_order_id": order_id}])

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert job_api.ledgers() == (), "被拒的请求不得留下台账"
