"""偏离标记的契约测试（tasks.md 4.3 的验证）。

事实来源：17-数据模型设计 §4.4（`Deviation` 实体 / 成因分类 / 状态）
          spec `transaction-base`「偏离批次标记」（场景：偏离写入 Deviation / 达标不写 Deviation）
          design.md D5（编排链：后验记录 → 偏离记录，同一事务）

三条要钉住的口径：

  1. **偏离写**：`verify_result = DEVIATION` 的指标落一条 `Deviation`，标识 = 物料 + 批号，
     `actual/threshold_cross_aisle` 取该指标自身的实测 / 阈值，成因按作业类型给 v1 默认值。
  2. **达标不写**：`verify_result = PASS` 的作业单不产生任何 `Deviation` 记录。
  3. **成因分派**：入库偏离 → 新入库收拢不达标；移库偏离 → 历史库存拖累（v1 默认，
     操作员可后续修订，`15-04` §4.1）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cap.deviation import refresh_material_deviations, scan_material_deviations
from app.core.enums import AccountStatus, JobStatus, JobType, Role
from app.models.identity import Account
from app.models.job import Deviation, DeviationCauseKind, DeviationStatus
from app.models.linkage import InventoryItem
from app.services.inbound import confirm_inbound
from app.services.kpi import list_deviations
from app.services.relocate import confirm_relocate
from app.services.verify import run_verification

from .conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"
MATERIAL = "M1"


def _operator(session: Session) -> Account:
    account = Account(
        warehouse_id=WAREHOUSE,
        username="gtj_keeper",
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    return account


def _deviations(session: Session) -> list[Deviation]:
    return list(session.scalars(select(Deviation).order_by(Deviation.id)))


# ------------------------------------------------------------------ 偏离写入 Deviation

def test_inbound_deviation_writes_deviation(session: Session) -> None:
    """入库把料落进第 6 条巷道（同批仍 ≤3）→ 一条 `Deviation`：物料口径 6>5、成因新入库收拢不达标。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 6)
        ],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="060101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    deviations = _deviations(session)
    assert len(deviations) == 1
    dev = deviations[0]
    assert dev.warehouse_id == WAREHOUSE
    assert dev.material_code == MATERIAL
    assert dev.batch_no == BATCH
    assert dev.actual_cross_aisle == 6
    assert dev.threshold_cross_aisle == 5
    assert dev.cause_kind is DeviationCauseKind.NEW_INBOUND_SHORTFALL
    assert dev.status is DeviationStatus.OPEN


def test_inbound_deviation_dedups_same_material_batch(session: Session) -> None:
    """同一 (物料, 批号, 成因, 阈值) 已有未处置偏离 → 后验不再重复落（重复入库单只落一条）。

    同一生产批号被多张入库单共享（`generate_batch_no` 同日同批），逐单后验会把「该批收拢
    不达标」这一条事实重复落 N 行 —— 真实数据里 4 张入库单共享 GJP2691571 即撞此况。回归
    钉住：两张同物料同批号的入库单都后验超标，`Deviation` 只落一条。
    """
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 7)  # 6 条巷道 → 同物料跨巷道 6 > 5
        ],
        job_orders=[
            JobOrderSpec(
                order_no=f"PO-0{i}",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
            for i in (1, 2)
        ],
    )
    order1, order2 = scenario.job_orders
    order1.status = JobStatus.VERIFY_FAILED
    order2.status = JobStatus.VERIFY_FAILED
    session.flush()

    run_verification(session, job_order=order1, snapshot=scenario.snapshot)
    run_verification(session, job_order=order2, snapshot=scenario.snapshot)

    assert order1.status is JobStatus.VERIFIED
    assert order2.status is JobStatus.VERIFIED
    deviations = _deviations(session)
    assert len(deviations) == 1
    assert deviations[0].material_code == MATERIAL
    assert deviations[0].batch_no == BATCH


# ------------------------------------------------------------------ 达标不写 Deviation

def test_inbound_pass_writes_no_deviation(session: Session) -> None:
    """入库落位收拢（1 条巷道）→ `VERIFIED` 但**不产生** `Deviation` 记录。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    assert _deviations(session) == []


# ------------------------------------------------------------------ 成因分派（移库 → 历史库存拖累）

def test_relocate_deviation_writes_legacy_cause(session: Session) -> None:
    """移库未收拢（跨巷道 2→2 持平）→ 一条 `Deviation`，成因历史库存拖累。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(location_code="010104", material_code=MATERIAL, batch_no=BATCH, qty=40),
            InventorySpec(location_code="010105", material_code=MATERIAL, batch_no=BATCH, qty=40),
            InventorySpec(location_code="020101", material_code=MATERIAL, batch_no=BATCH, qty=40),
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_locations=[{"location_code": "010104", "qty": 40}],
        target_location_code="020101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    deviations = _deviations(session)
    assert len(deviations) == 1
    dev = deviations[0]
    assert dev.actual_cross_aisle == 2
    assert dev.threshold_cross_aisle == 2
    assert dev.cause_kind is DeviationCauseKind.LEGACY_INVENTORY_DRAG


# ------------------------------------------------------------------ 偏离清单（移库任务来源，kpi.list_deviations）

def test_list_deviations_filters_by_status(session: Session) -> None:
    """`kpi.list_deviations` 列出本仓偏离清单，`status` 可选过滤（默认全部）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 6)
        ],
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        target_location_code="060101",
        snapshot=scenario.snapshot,
    )

    assert [d.id for d in list_deviations(session, warehouse_id=WAREHOUSE)] == [
        d.id for d in _deviations(session)
    ]
    assert list_deviations(session, warehouse_id=WAREHOUSE, status=DeviationStatus.OPEN)
    assert list_deviations(session, warehouse_id=WAREHOUSE, status=DeviationStatus.IMPROVED) == []
    assert list_deviations(session, warehouse_id="OTHER-WAREHOUSE") == []


# ------------------------------------------------------------------ 全量偏离扫描（18 §1.5「导入」触发路径）

def test_scan_material_deviations_marks_scattered_material(session: Session) -> None:
    """同物料跨巷道 >5 的物料落一条物料级 Deviation（批号留空，成因历史库存拖累）；≤5 不落。"""
    scenario = make_scenario(
        session,
        inventory=[
            *[
                InventorySpec(
                    location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
                )
                for i in range(1, 7)  # 6 条巷道 → 跨巷道 6 > 5
            ],
            # 收拢物料：只占 2 条巷道 → 不落偏离
            InventorySpec(location_code="010101", material_code="M2", batch_no="LEGACY", qty=10),
            InventorySpec(location_code="020101", material_code="M2", batch_no="LEGACY", qty=10),
        ],
    )

    created = scan_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    assert [d.material_code for d in created] == [MATERIAL]
    dev = created[0]
    assert dev.batch_no is None
    assert dev.actual_cross_aisle == 6
    assert dev.threshold_cross_aisle == 5
    assert dev.cause_kind is DeviationCauseKind.LEGACY_INVENTORY_DRAG
    assert dev.status is DeviationStatus.OPEN


def test_scan_material_deviations_idempotent(session: Session) -> None:
    """重复扫描只补缺：第二次扫描不新增（同物料已有一条偏离即跳过）。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 7)
        ],
    )

    first = scan_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )
    second = scan_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    assert len(first) == 1
    assert second == []
    assert len(_deviations(session)) == 1


def test_relocate_refreshes_material_deviation_to_improved(session: Session) -> None:
    """移库把物料收拢到阈值以内 → 物料级偏离跨巷道数刷新、状态「已发起移库 → 已改善」。

    回归（Problem：集中度 KPI 报表，物料已按推荐完成移库、跨巷道数仍停在移库前旧值）：
    `scan_material_deviations` 落库的 `actual_cross_aisle` 是静态快照，移库完成后后验编排
    必须把它刷成收拢后的真实值，并在 ≤ 阈值时推进到「已改善」—— 否则 P6 一直显示移库前的数。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=40
            )
            for i in range(1, 7)  # 6 条巷道 → 同物料跨巷道 6 > 5
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    # 物料级偏离已由「发起移库」迁到「已发起移库」（relocate_job_order_id 指向本单）。
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            material_code=MATERIAL,
            batch_no=None,
            actual_cross_aisle=6,
            threshold_cross_aisle=5,
            cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
            status=DeviationStatus.RELOCATE_STARTED,
            relocate_job_order_id=order.id,
        )
    )
    session.flush()

    # 把 02 巷并回 01 巷 → 6 条巷道收拢到 5 条（≤ 阈值 5）。
    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_locations=[{"location_code": "020101", "qty": 40}],
        target_location_code="010101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    (dev,) = _deviations(session)
    assert dev.batch_no is None
    assert dev.actual_cross_aisle == 5
    assert dev.status is DeviationStatus.IMPROVED


def test_relocate_refreshes_batch_level_deviation_too(session: Session) -> None:
    """批号级偏离（后验逐单落的那类）发起移库后同样刷新跨巷道数、推进「已改善」。

    `start_relocate` 是唯一写「已发起移库」的入口，它把偏离一律当作「该物料要按物料
    收拢散批」的物料级事实（按 `material_code` 扇出散批），故批号级偏离一旦发起移库，
    其语义也归一为「该物料跨巷道超标」—— 收拢后按物料重算并对齐状态（真实数据里的
    偏离 #3：批号 GJP2691571、跨巷道 28，物料收拢后只剩 3 巷，即撞此况）。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=40
            )
            for i in range(1, 7)  # 6 条巷道 → 同物料跨巷道 6 > 5
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    # 批号级偏离（batch_no 非空，后验逐单落库形态），已发起移库。
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            material_code=MATERIAL,
            batch_no=BATCH,
            actual_cross_aisle=6,
            threshold_cross_aisle=5,
            cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
            status=DeviationStatus.RELOCATE_STARTED,
            relocate_job_order_id=order.id,
        )
    )
    session.flush()

    # 把 02 巷并回 01 巷 → 6 条巷道收拢到 5 条（≤ 阈值 5）。
    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_locations=[{"location_code": "020101", "qty": 40}],
        target_location_code="010101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    (dev,) = _deviations(session)
    assert dev.batch_no == BATCH
    assert dev.actual_cross_aisle == 5
    assert dev.status is DeviationStatus.IMPROVED


def test_relocate_keeps_material_deviation_started_when_still_scattered(session: Session) -> None:
    """移库收拢了但仍超阈值（7→6）→ 跨巷道数刷新、状态仍「已发起移库」（可再发起收拢）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=40
            )
            for i in range(1, 8)  # 7 条巷道 → 同物料跨巷道 7 > 5
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            material_code=MATERIAL,
            batch_no=None,
            actual_cross_aisle=7,
            threshold_cross_aisle=5,
            cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
            status=DeviationStatus.RELOCATE_STARTED,
            relocate_job_order_id=order.id,
        )
    )
    session.flush()

    # 把 02 巷并回 01 巷 → 7 条巷道收拢到 6 条（仍 > 阈值 5）。
    confirm_relocate(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        source_locations=[{"location_code": "020101", "qty": 40}],
        target_location_code="010101",
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    (dev,) = _deviations(session)
    assert dev.batch_no is None
    assert dev.actual_cross_aisle == 6
    assert dev.status is DeviationStatus.RELOCATE_STARTED


def test_scan_material_deviations_skips_material_with_existing_deviation(session: Session) -> None:
    """某物料已有一条 Deviation（后验触发、带批号）→ 全量扫描不重复落。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 7)
        ],
    )
    # 预置一条后验触发的偏离（带批号）—— 模拟 verify._write_deviations 已落过。
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            material_code=MATERIAL,
            batch_no=BATCH,
            actual_cross_aisle=6,
            threshold_cross_aisle=5,
            cause_kind=DeviationCauseKind.NEW_INBOUND_SHORTFALL,
        )
    )
    session.flush()

    created = scan_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    assert created == []
    assert len(_deviations(session)) == 1  # 仍只有预置的那一条


# ------------------------------------------------------------------ 最新偏离批次表刷新（GET /deviation 读路径，18 §1.5）

def test_refresh_material_deviations_recomputes_cross_aisle_from_inventory(
    session: Session,
) -> None:
    """物料偏离的 `actual_cross_aisle` 随库存刷新：出库清空后从 6 巷刷成 1 巷、推进「已改善」。

    Problem（用户提的 2）：KPI 看板偏离批次表要「最新」。`scan_material_deviations` 落库的
    `actual_cross_aisle` 是静态快照，出库 / 移库改了库存分布后不刷就停在旧值 ——
    `list_deviation` 读之前调本函数，用当前快照库存重算。
    """
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 7)  # 6 条巷道 → 跨巷道 6 > 5
        ],
    )
    scan_material_deviations(session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id)
    session.flush()
    (dev,) = _deviations(session)
    assert dev.actual_cross_aisle == 6
    assert dev.status is DeviationStatus.OPEN

    # 出库清空 5 条巷道，只剩 01 巷 → 刷新后跨巷道 1、状态已改善。
    for item in session.scalars(select(InventoryItem)).all():
        if item.location_code != "010101":
            session.delete(item)
    session.flush()

    refreshed = refresh_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    assert [d.actual_cross_aisle for d in refreshed] == [1]
    assert [d.status for d in refreshed] == [DeviationStatus.IMPROVED]


def test_refresh_material_deviations_keeps_open_when_still_scattered(session: Session) -> None:
    """库存未变（仍 6 巷 >5）→ 刷新后 `actual_cross_aisle` 不变、状态仍「未处理」。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no="LEGACY", qty=10
            )
            for i in range(1, 7)
        ],
    )
    scan_material_deviations(session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id)

    refreshed = refresh_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    assert [d.actual_cross_aisle for d in refreshed] == [6]
    assert [d.status for d in refreshed] == [DeviationStatus.OPEN]


def test_refresh_material_deviations_preserves_relocate_started(session: Session) -> None:
    """「已发起移库」的偏离刷新跨巷道数，但状态不覆盖（移库在途，推进由后验负责）。"""
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec(
                location_code=f"0{i}0101", material_code=MATERIAL, batch_no=BATCH, qty=40
            )
            for i in range(1, 7)
        ],
        job_orders=[
            JobOrderSpec(
                order_no="MV-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.RELOCATE,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    session.add(
        Deviation(
            warehouse_id=WAREHOUSE,
            material_code=MATERIAL,
            batch_no=None,
            actual_cross_aisle=6,
            threshold_cross_aisle=5,
            cause_kind=DeviationCauseKind.LEGACY_INVENTORY_DRAG,
            status=DeviationStatus.RELOCATE_STARTED,
            relocate_job_order_id=order.id,
        )
    )
    session.flush()

    refreshed = refresh_material_deviations(
        session, warehouse_id=WAREHOUSE, snapshot_id=scenario.snapshot.id
    )

    (dev,) = refreshed
    assert dev.actual_cross_aisle == 6
    assert dev.status is DeviationStatus.RELOCATE_STARTED
