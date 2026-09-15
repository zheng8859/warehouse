"""顺路取只读派生（tasks.md 1.1）与出库后验读台账拣货路径（tasks.md 4.2）的契约测试。

事实来源：15-03 §3.2（顺路取 = 按巷道聚合既有库存）、§8.1（加权集中度 80% ≤ N）
          17 §10.2（PickSequence 形状：do_no / pick_sequence / weighted_concentration /
              threshold_n / exceeded）
          spec `transaction-base`「顺路取只读派生」「同步后验与三口径判定」
          design.md D1（纯函数：不触会话、不调 engine.invoke、不写 InventoryItem/Ledger）、
            D5（出库后验读台账拣货路径 `pick_path_json`，回退单源）

前半钉的是 `derive_pick_sequence`（纯函数）：

  1. **FIFO + 集中 + 封顶**：按批号升序（FIFO，批号字典序 = 生产日期序）逐批贪心，批内按
     「单巷量从大到小、并列巷号升序」取巷，拣货量封顶到订单 `qty`；最早批多巷且各巷都够
     交货量时只出一条巷。
  2. **`batch_no` = FIFO 最早批**：结果里带 `batch_no`（端点回写 `order.batch_no`，
     不进方案 payload）。
  3. **加权集中度**复用 `concentration_aisle_count`（对拣货分布算 80% 降序累加），
     `threshold_n` 默认 5，`exceeded = weighted_concentration > n`，仅高亮不阻断。
  4. **空档案合法**：`qty_by_batch_by_aisle` 空 → 空 `pick_sequence`、`batch_no=None`、
     集中度 0（货未入库由端点分列 `not_in_stock`，不是本函数的事）。
  5. **确定性**：同样 profile + qty 两次同输出（无随机、无大模型）。

后半钉的是 `_pick_qty_from_ledger`（出库后验的取数源）：

  5. **多巷聚合**：`pick_path_json` 逐 `aisle` 累加 `qty` → `{aisle: qty}`，再喂
     `verify_outbound` 算集中度（≤N PASS / >N DEVIATION，超标仅标记不阻断出库）。
  6. **回退单源**：`pick_path_json` 缺失/空且台账有 `source_location_code` → 单巷
     `{aisle_of(source): qty}`（历史单源出库单，恒集中度 1）。
  7. **两者皆无 → `ValidationBlocked`**：迁 `VERIFY_FAILED` 待重试，不静默 PASS。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AccountStatus, JobStatus, JobType, LedgerType, Role, VerifyResult
from app.core.errors import ValidationBlocked
from app.engine.factors import InventoryProfile
from app.models.identity import Account
from app.models.job import JobOrder, Ledger, Verification
from app.services.outbound import confirm_outbound, derive_pick_sequence
from app.services.verify import _pick_qty_from_ledger, run_verification

from .conftest import InventorySpec, JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

MATERIAL = "M1"
DO_NO = "DO-20260908-001"
WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"


def _profile(
    *,
    qty_by_batch_by_aisle: dict[str, dict[str, int]],
) -> InventoryProfile:
    """按「批号 → 巷道 → 数量」直接造一份单料号档案。

    `derive_pick_sequence` 只读 `qty_by_batch_by_aisle`（FIFO 按批、按巷拆量），不读
    `plates_by_aisle` / `batches_by_aisle`（那两处仍是分配因子的取数），故这里只给批号级
    分布 —— 不必走 `make_scenario`（那是给读快照/写库的用例准备的）。
    """
    return InventoryProfile(
        snapshot_present=True,
        qty_by_batch_by_aisle=qty_by_batch_by_aisle,
    )


def test_derive_single_batch_single_aisle_caps_to_qty() -> None:
    """单批单巷 100 板、交货 40 → 只拣 40，不把整份库存 dump 成拣货量。"""
    profile = _profile(qty_by_batch_by_aisle={"GJP2571221": {"01": 100}})

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=40
    )

    assert result["do_no"] == DO_NO
    assert result["batch_no"] == "GJP2571221"
    assert result["pick_sequence"] == [
        {"aisle": "01", "qty": 40, "batches": ["GJP2571221"], "locations": []}
    ]


def test_derive_fifo_earliest_batch_picked_first() -> None:
    """最早批在 01 巷、较新批在 02 巷：交货 5 → 只从最早批拣，`batch_no` = 最早批。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 10},  # 最早批
            "GJP2690871": {"02": 100},  # 较新批
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=5
    )

    assert result["batch_no"] == "GJP2571221"
    assert result["pick_sequence"] == [
        {"aisle": "01", "qty": 5, "batches": ["GJP2571221"], "locations": []}
    ]


def test_derive_single_aisle_when_earliest_batch_in_multiple_sufficient_aisles() -> None:
    """最早批在两条巷、每巷都够交货量 → 只显示一条巷（单巷量大者、并列取巷号小）。"""
    profile = _profile(qty_by_batch_by_aisle={"GJP2571221": {"01": 100, "02": 100}})

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=50
    )

    assert result["batch_no"] == "GJP2571221"
    assert result["pick_sequence"] == [
        {"aisle": "01", "qty": 50, "batches": ["GJP2571221"], "locations": []}
    ]


def test_derive_continues_to_next_batch_when_earliest_insufficient() -> None:
    """最早批 30+3=33 不足交货 50 → 顺延下一批（仍 FIFO），`batch_no` 仍是最近早批。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 30, "02": 3},
            "GJP2571321": {"03": 100},
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=50
    )

    assert result["batch_no"] == "GJP2571221"
    assert result["pick_sequence"] == [
        {"aisle": "01", "qty": 30, "batches": ["GJP2571221"], "locations": []},
        {"aisle": "02", "qty": 3, "batches": ["GJP2571221"], "locations": []},
        {"aisle": "03", "qty": 17, "batches": ["GJP2571321"], "locations": []},
    ]


def test_derive_merges_same_aisle_across_batches() -> None:
    """最早批 10 板在 01 巷、次批也在 01 巷（量更大）：同巷合并成一条，批号升序。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 10},
            "GJP2571321": {"01": 100, "02": 100},
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=50
    )

    assert result["batch_no"] == "GJP2571221"
    assert result["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 50,
            "batches": ["GJP2571221", "GJP2571321"],
            "locations": [],
        }
    ]


def test_derive_resolves_locations_fifo_then_location_order() -> None:
    """缺口 3：传入 `batch_locations_by_aisle` 时把每条被拣巷道下钻到逐格库位号。

    FIFO 批序 → 批内库位号升序逐个消费，末巷/末格按拣货量截断。例子即交付说明的 DO-88：
    最早批 `GJP2509011` 巷01=60（010101·30 / 010102·30）、巷02=50（020101·50），交货 100
    → 巷01 拣 60、巷02 拣 40，库位逐格 010101·30 / 010102·30 / 020101·40。
    """
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2509011": {"01": 60, "02": 50},
            "GJP2509022": {"01": 40},
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL,
        do_no=DO_NO,
        profile=profile,
        qty=100,
        batch_locations_by_aisle={
            "GJP2509011": {"01": [("010101", 30), ("010102", 30)], "02": [("020101", 50)]},
            "GJP2509022": {"01": [("010103", 40)]},
        },
    )

    assert result["batch_no"] == "GJP2509011"
    assert result["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 60,
            "batches": ["GJP2509011"],
            "locations": [
                {"location_code": "010101", "qty": 30, "batch_no": "GJP2509011"},
                {"location_code": "010102", "qty": 30, "batch_no": "GJP2509011"},
            ],
        },
        {
            "aisle": "02",
            "qty": 40,
            "batches": ["GJP2509011"],
            "locations": [
                {"location_code": "020101", "qty": 40, "batch_no": "GJP2509011"},
            ],
        },
    ]


def test_derive_resolves_locations_truncates_single_cell() -> None:
    """单库位 10 箱但只拣 5 → 该库位 `qty` 截断为 5，不把整格库存 dump 成拣货量。"""
    profile = _profile(qty_by_batch_by_aisle={"GJP2571221": {"01": 5}})

    result = derive_pick_sequence(
        material_code=MATERIAL,
        do_no=DO_NO,
        profile=profile,
        qty=5,
        batch_locations_by_aisle={"GJP2571221": {"01": [("010101", 10)]}},
    )

    assert result["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 5,
            "batches": ["GJP2571221"],
            "locations": [{"location_code": "010101", "qty": 5, "batch_no": "GJP2571221"}],
        }
    ]


def test_derive_resolves_locations_across_batches_in_fifo_order() -> None:
    """同巷多批合并：`locations` 按 FIFO 批序展开，批号各自标注。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 10},
            "GJP2571321": {"01": 100},
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL,
        do_no=DO_NO,
        profile=profile,
        qty=50,
        batch_locations_by_aisle={
            "GJP2571221": {"01": [("010101", 10)]},
            "GJP2571321": {"01": [("010102", 100)]},
        },
    )

    assert result["pick_sequence"] == [
        {
            "aisle": "01",
            "qty": 50,
            "batches": ["GJP2571221", "GJP2571321"],
            "locations": [
                {"location_code": "010101", "qty": 10, "batch_no": "GJP2571221"},
                {"location_code": "010102", "qty": 40, "batch_no": "GJP2571321"},
            ],
        }
    ]


def test_derive_weighted_concentration_on_capped_pick() -> None:
    """拣货分布 974 / 43 / 3 → 80% 的 816 被 974 一条覆盖 → 集中度 1，不超 N。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 43, "02": 3},
            "GJP2571321": {"03": 974},
        }
    )

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=1020
    )

    assert result["weighted_concentration"] == 1
    assert result["threshold_n"] == 5
    assert result["exceeded"] is False


def test_derive_exceeded_highlights_only() -> None:
    """交货量跨 7 条巷道、每条 10 板 → 80% 需 6 条巷 → 6 > 5，仅高亮不阻断。"""
    profile = _profile(
        qty_by_batch_by_aisle={f"GJP25712{i}{i}": {f"0{i}": 10} for i in range(1, 8)}
    )

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=70
    )

    assert result["weighted_concentration"] == 6
    assert result["exceeded"] is True


def test_derive_empty_profile_is_empty_sequence() -> None:
    """空档案（货未入库）→ 空 pick_sequence、`batch_no=None`、集中度 0。"""
    profile = _profile(qty_by_batch_by_aisle={})

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=10
    )

    assert result["pick_sequence"] == []
    assert result["batch_no"] is None
    assert result["weighted_concentration"] == 0
    assert result["exceeded"] is False


def test_derive_deterministic() -> None:
    """同样 profile + qty 两次同输出（「同样输入必得同样输出」）。"""
    profile = _profile(
        qty_by_batch_by_aisle={
            "GJP2571221": {"01": 30, "02": 3},
            "GJP2571321": {"03": 100},
        }
    )

    first = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=50
    )
    second = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=50
    )

    assert first == second


def test_derive_default_threshold_n() -> None:
    """未传 n → `threshold_n` 取 `DEFAULT_CONCENTRATION_N`（5）。"""
    profile = _profile(qty_by_batch_by_aisle={"GJP2571221": {"01": 3}})

    result = derive_pick_sequence(
        material_code=MATERIAL, do_no=DO_NO, profile=profile, qty=3
    )

    assert result["threshold_n"] == 5


# ------------------------------------------------------------------ 出库后验读台账拣货路径（tasks.md 4.2）

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


def _verification(session: Session, order: JobOrder) -> Verification | None:
    return session.scalars(
        select(Verification).where(Verification.job_order_id == order.id)
    ).first()


def _ledger(
    session: Session,
    order: JobOrder,
    *,
    operator_id: int,
    source_location_code: str | None = None,
    pick_path_json: list | None = None,
) -> Ledger:
    """直接落一条出库台账行（无源无拣货路径的形态由参数表达）。

    走 `Ledger` 构造而非 `confirm_outbound`：`_pick_qty_from_ledger` 的「回退单源」与
    「两者皆无」两个分支，在 D4 之后已无法经确认编排触达（出库确认恒 source=None），
    故这里手摆历史形态的台账行来钉住读口径。
    """
    ledger = Ledger(
        warehouse_id=order.warehouse_id,
        job_order_id=order.id,
        is_reversal=False,
        ledger_type=LedgerType.OUTBOUND,
        order_no=order.order_no,
        material_code=order.material_code,
        batch_no=order.batch_no,
        qty=order.actual_qty if order.actual_qty is not None else order.qty,
        source_location_code=source_location_code,
        target_location_code=None,
        pick_path_json=pick_path_json,
        operator_id=operator_id,
        executed_at=NOW,
    )
    session.add(ledger)
    session.flush()
    return ledger


def test_verify_outbound_multi_aisle_concentration_pass(session: Session) -> None:
    """多巷 `pick_path_json` 聚合 → 集中度 2（≤5）→ PASS。

    60/25/10/5 四条巷道：80% 的 100 是 80，60 不够、加 25 到 85 够 → 覆盖 2 条巷道。
    `_pick_qty_from_ledger` 把拣货路径按 `aisle` 聚回 `{01: 60, 02: 25, 03: 10, 04: 5}`，
    `verify_outbound` 算得 2 —— 与「出库确认记录最终拣货路径」的巷道粒度口径一致。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[
            InventorySpec("010101", MATERIAL, BATCH, 60),
            InventorySpec("020101", MATERIAL, BATCH, 25),
            InventorySpec("030101", MATERIAL, BATCH, 10),
            InventorySpec("040101", MATERIAL, BATCH, 5),
        ],
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=100,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_outbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        pick_path_json=[
            {"aisle": "01", "qty": 60, "batches": [BATCH]},
            {"aisle": "02", "qty": 25, "batches": [BATCH]},
            {"aisle": "03", "qty": 10, "batches": [BATCH]},
            {"aisle": "04", "qty": 5, "batches": [BATCH]},
        ],
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    verification = _verification(session, order)
    assert verification.metric_kind == "拣货量加权集中度"
    assert verification.actual_value == 2
    assert verification.verify_result is VerifyResult.PASS


def test_verify_outbound_multi_aisle_concentration_deviation(session: Session) -> None:
    """7 巷各 10 板 → 80% 的 56 需 6 条巷 → 集中度 6（>5）→ DEVIATION（仅标记不阻断）。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec(f"0{i}0101", MATERIAL, BATCH, 10) for i in range(1, 8)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=70,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    confirm_outbound(
        session,
        job_order=order,
        operator_id=operator.id,
        executed_at=NOW,
        pick_path_json=[
            {"aisle": f"0{i}", "qty": 10, "batches": [BATCH]} for i in range(1, 8)
        ],
        snapshot=scenario.snapshot,
    )

    assert order.status is JobStatus.VERIFIED
    verification = _verification(session, order)
    assert verification.verify_result is VerifyResult.DEVIATION
    assert verification.actual_value == 6


def test_pick_qty_from_ledger_falls_back_to_source(session: Session) -> None:
    """台账无 `pick_path_json` 但有源库位 → 回退单巷 `{aisle_of(source): qty}`。

    历史单源出库单（无拣货路径）只有「一个源库位」这一条线索，聚合结果是单巷 ——
    集中度恒 1，由 `verify_outbound` 复用同一口径（这里直接断言取数形态）。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    _ledger(session, order, operator_id=operator.id, source_location_code="010104")

    assert _pick_qty_from_ledger(session, order) == {"01": 40}


def test_verify_outbound_missing_path_and_source_verify_failed(session: Session) -> None:
    """台账既无拣货路径也无源库位 → `ValidationBlocked` → 迁 `VERIFY_FAILED`（不静默 PASS）。

    「算不出来」与「算出来超标」是两回事：前者是数据自相矛盾，后验迁 `VERIFY_FAILED`
    待重试（`15-01` §3.3.2），不是给一个假达标。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]
    order.status = JobStatus.EXECUTED
    _ledger(session, order, operator_id=operator.id)

    result = run_verification(session, job_order=order, snapshot=scenario.snapshot)

    assert result.status is JobStatus.VERIFY_FAILED
    assert _verification(session, order) is None


# ------------------------------------------------------------------ 拣货量不足订单量 → 阻断过账

def test_confirm_outbound_blocks_when_pick_short_of_order(session: Session) -> None:
    """拣货路径总量 < 订单量 → `ValidationBlocked` 阻断，不写台账、不扣库存。

    Problem（P6 KPI / 用户提的 1）：出货单 20250530 已过账 10200 箱但顺路取只覆盖 834 箱。
    根因：`derive_pick_sequence` 只在可用库存里封顶、不标「库存不足」（`exceeded` 是加权
    集中度 > 5 巷，与库存是否充足无关），确认编排若不拦，台账会按满额 `job_order.qty`
    记出库、扣减却按拣货路径扣，账实不一致。这里钉住「库存不足不得带病过账」。
    """
    operator = _operator(session)
    scenario = make_scenario(
        session,
        inventory=[InventorySpec("010101", MATERIAL, BATCH, 40)],
        job_orders=[
            JobOrderSpec(
                order_no="DO-88",
                material_code=MATERIAL,
                qty=100,
                batch_no=BATCH,
                job_type=JobType.OUTBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    order = scenario.job_orders[0]

    with pytest.raises(ValidationBlocked):
        confirm_outbound(
            session,
            job_order=order,
            operator_id=operator.id,
            executed_at=NOW,
            pick_path_json=[{"aisle": "01", "qty": 40, "batches": [BATCH]}],
            snapshot=scenario.snapshot,
        )

    # 阻断：状态停在 PLANNED、零台账（未确认不产生台账，CLAUDE.md §四）。
    assert order.status is JobStatus.PLANNED
    assert session.scalars(select(Ledger)).all() == []
