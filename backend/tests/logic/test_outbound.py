"""顺路取只读派生（tasks.md 1.1）与出库后验读台账拣货路径（tasks.md 4.2）的契约测试。

事实来源：15-03 §3.2（顺路取 = 按巷道聚合既有库存）、§8.1（加权集中度 80% ≤ N）
          17 §10.2（PickSequence 形状：do_no / pick_sequence / weighted_concentration /
              threshold_n / exceeded）
          spec `transaction-base`「顺路取只读派生」「同步后验与三口径判定」
          design.md D1（纯函数：不触会话、不调 engine.invoke、不写 InventoryItem/Ledger）、
            D5（出库后验读台账拣货路径 `pick_path_json`，回退单源）

前半钉的是 `derive_pick_sequence`（纯函数）：

  1. **现状分布**：`pick_sequence` 取自 `InventoryProfile.plates_by_aisle`（每巷库存量 =
     拣货量，不跨巷分配）+ `batches_by_aisle`（每巷批号集），按 `aisle` 升序。
  2. **加权集中度**复用 `concentration_aisle_count`（80% 降序累加），`threshold_n` 默认 5，
     `exceeded = weighted_concentration > n`，仅高亮不阻断。
  3. **空档案合法**：`plates_by_aisle` 空 → 空 `pick_sequence`、集中度 0（货未入库由端点
     分列 `not_in_stock`，不是本函数的事）。
  4. **确定性**：同样 profile 两次同输出（无随机、无大模型）。

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
    plates: dict[str, int],
    batches: dict[str, frozenset[str]] | None = None,
) -> InventoryProfile:
    """按「每巷板数」直接造一份单料号档案；批号集缺省 = 该巷板数 > 0 就给一个批号。

    `derive_pick_sequence` 是纯函数，不读会话，故直接用 `InventoryProfile` 造数即可 ——
    不必走 `make_scenario`（那是给读快照/写库的用例准备的）。
    """
    if batches is None:
        batches = {aisle: frozenset({f"B-{aisle}"}) for aisle in plates}
    return InventoryProfile(
        snapshot_present=True,
        plates_by_aisle=plates,
        batches_by_aisle=batches,
    )


def test_derive_aggregates_by_aisle_ascending() -> None:
    """按巷道升序聚合每巷 qty + batches，do_no 原样带回。"""
    profile = _profile(
        plates={"03": 10, "01": 3, "02": 7},
        batches={"03": frozenset({"B3"}), "01": frozenset({"B1"}), "02": frozenset({"B2"})},
    )

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert result["do_no"] == DO_NO
    assert result["pick_sequence"] == [
        {"aisle": "01", "qty": 3, "batches": ["B1"]},
        {"aisle": "02", "qty": 7, "batches": ["B2"]},
        {"aisle": "03", "qty": 10, "batches": ["B3"]},
    ]


def test_derive_batches_sorted_for_determinism() -> None:
    """同一巷多个批号 → batches 升序（frozenset 无序，排序保证确定性）。"""
    profile = _profile(
        plates={"01": 5},
        batches={"01": frozenset({"B-C", "B-A", "B-B"})},
    )

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert result["pick_sequence"][0]["batches"] == ["B-A", "B-B", "B-C"]


def test_derive_weighted_concentration_and_not_exceeded() -> None:
    """60/25/10/5 → 80% 的 100 是 80：60 不够、加 25 到 85 够 → 覆盖 2 巷道，未超标。"""
    profile = _profile(plates={"01": 60, "02": 25, "03": 10, "04": 5})

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert result["weighted_concentration"] == 2
    assert result["threshold_n"] == 5
    assert result["exceeded"] is False


def test_derive_exceeded_highlights_only() -> None:
    """7 巷道各 10 板 → 80% 的 56 需累加 6 条巷道 → 6 > 5，`exceeded=True`（仅高亮）。"""
    profile = _profile(plates={f"0{i}": 10 for i in range(1, 8)})

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert result["weighted_concentration"] == 6
    assert result["exceeded"] is True


def test_derive_empty_profile_is_empty_sequence() -> None:
    """空档案（货未入库）→ 空 pick_sequence、集中度 0、不超标（不 crash）。"""
    profile = _profile(plates={})

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert result["pick_sequence"] == []
    assert result["weighted_concentration"] == 0
    assert result["exceeded"] is False


def test_derive_deterministic() -> None:
    """同样 profile 两次同输出（「同样输入必得同样输出」）。"""
    profile = _profile(plates={"01": 3, "02": 7})

    first = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)
    second = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

    assert first == second


def test_derive_default_threshold_n() -> None:
    """未传 n → `threshold_n` 取 `DEFAULT_CONCENTRATION_N`（5）。"""
    profile = _profile(plates={"01": 3})

    result = derive_pick_sequence(material_code=MATERIAL, do_no=DO_NO, profile=profile)

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
