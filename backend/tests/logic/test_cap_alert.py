"""`CapAlert` 的取值域与「引用恰好一个来源」契约（tasks 3.5 的验证）。

事实来源：17-数据模型设计 §3.4 · 16-数据衔接与 cap 自维护 §6.5 / §11.7
          openspec/changes/data-model-permission/design.md D2、D5
          spec `data-model`「不可变快照基线与 cap 引用」场景「CapAlert 引用恰好一个来源」

**为什么这条进 `tests/logic/` 而不是 `tests/models/`**：`alert_kind` 与「恰好一个来源」
不是单纯的 CRUD 约束，而是 16 §6.5 四类 cap 异常在本模型里的落点 —— 负 cap / 超总格 /
漂移超阈值由**快照重算**发现（引用快照），增量失败由**台账事务**发现（引用事务号）。
两个都空 = 无法定位的告警，两个都非空 = 不知道该以谁为准（D5）。三层 TDD 的分工里
这类「口径判定」归逻辑层（00-总体开发方案 §3.1）。

**§4 已补上 `ledger_txn_id` 的外键，本文件随之收紧**（§3 当时留的口子，见 tasks.md 9.4b）：
`ledgers` 表属作业链，§3 落地 `CapAlert` 时它还不存在，故那一版先落整数列。§4 补上后

  - 「只引用台账事务」的用例不再随手编一个 `ledger_txn_id=42`，而是**造一行真实台账**；
  - 「两个都非空」的用例现在有两个约束会命中，断言收紧为「命中的是 CHECK 而不是外键」——
    这条区分是有意义的：若它撞的是外键，说明「两个来源恰好一个」这条口径**已经被绕过**，
    外键只是碰巧挡住了同一行数据。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.enums import ImportStatus, JobStatus, JobType, LedgerType
from app.models.base import Base
from app.models.job import JobOrder, Ledger
from app.models.linkage import AlertKind, CapAlert, ImportSession, Snapshot

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = "2026-09-11 08:00:00"
DATA_TIME = datetime(2026, 9, 8, 0, 0)


def _snapshot(session: Session) -> Snapshot:
    """最小父链：会话 → 快照（`snapshots` 无其他必填父行）。"""
    import_session = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="B-20260908",
        data_time=DATA_TIME,
        status=ImportStatus.DRAFT,
    )
    session.add(import_session)
    session.flush()

    snapshot = Snapshot(
        warehouse_id=WAREHOUSE,
        snapshot_time=DATA_TIME,
        version_no=1,
        import_session_id=import_session.id,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _ledger(session: Session) -> Ledger:
    """最小父链：作业单 → 台账。台账事务号就是 `Ledger.id`。

    §3 的 `CapAlert.ledger_txn_id` 只是个整数；§4 之后它是真外键，指向的行必须存在。
    """
    job_order = JobOrder(
        warehouse_id=WAREHOUSE,
        order_no="3573743144K55G",
        line_no="10",
        job_type=JobType.INBOUND,
        material_code="3001234",
        qty=40,
        status=JobStatus.EXECUTED,
    )
    session.add(job_order)
    session.flush()

    ledger = Ledger(
        warehouse_id=WAREHOUSE,
        job_order_id=job_order.id,
        ledger_type=LedgerType.INBOUND,
        order_no=job_order.order_no,
        material_code=job_order.material_code,
        batch_no="GJP2571221",
        qty=job_order.qty,
        operator_id=1,  # 占位整数：账号表属 §5，本列此刻无外键可指
        target_location_code="010104",
        executed_at=DATA_TIME,
    )
    session.add(ledger)
    session.flush()
    return ledger


def _alert(session: Session, snapshot: Snapshot, **overrides) -> CapAlert:
    fields = {
        "alert_kind": AlertKind.NEGATIVE_CAP,
        "aisle_no": "01",
        "snapshot_id": snapshot.id,
        "detail": "巷道 01 已占格数 > 总格数（16 §6.5 负 cap）",
    }
    fields.update(overrides)
    row = CapAlert(warehouse_id=WAREHOUSE, **fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _raw_insert(session: Session, **values) -> None:
    """原生 INSERT —— CHECK 类用例必须绕过 ORM（D3 的理由，见 test_linkage.py docstring）。"""
    fields = {
        "warehouse_id": WAREHOUSE,
        "alert_kind": "负cap",
        "aisle_no": "01",
        "snapshot_id": None,
        "ledger_txn_id": None,
        "detail": "x",
        "handled": 0,
        "created_at": NOW,
    }
    fields.update(values)
    session.execute(
        text(
            "INSERT INTO cap_alerts "
            "(warehouse_id, alert_kind, aisle_no, snapshot_id, ledger_txn_id, detail, "
            " handled, created_at) "
            "VALUES (:warehouse_id, :alert_kind, :aisle_no, :snapshot_id, :ledger_txn_id, "
            " :detail, :handled, :created_at)"
        ),
        fields,
    )


# ------------------------------------------------------------------ alert_kind 局部值域

def test_alert_kind_is_an_entity_local_domain() -> None:
    """D2 / spec：`CapAlert` 的告警类型是**实体局部值域**，不进 `enums.py`。

    17 §九 只登记跨模块共享的 11 个。把局部值域塞进去，会让「11 枚举」这个
    可核对的口径失效，也把「取值随各自实体章节定义」变成一句空话。
    """
    assert AlertKind.__module__ == "app.models.linkage"
    assert not hasattr(shared_enums, "AlertKind")


def test_alert_kind_values_match_16_section_6_5() -> None:
    """四个取值 = 16 §6.5 的四类 cap 异常，逐字对应（D5 的取值域）。

    存的是中文原文而不是代号：它们会直接出现在回执与看板的「告警项」里
    （17 §10.4、`/api/cap/alerts`），多一层代号映射只是多一个翻译点。
    """
    assert {k.value for k in AlertKind} == {"负cap", "超总格", "增量失败", "漂移超阈值"}
    # 成员名 ASCII、取值照文档 —— 与 enums.py 的 ItemStatus 同一处理。
    assert AlertKind.INCREMENT_FAILED.value == "增量失败"


def test_alert_kind_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层：`validate_strings=True` 在绑定参数时即抛 `StatementError`。"""
    snapshot = _snapshot(session)
    with pytest.raises(StatementError):
        _alert(session, snapshot, alert_kind="不存在的告警")


def test_alert_kind_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：绕过 ORM 的写入由 DB CHECK 拒绝。错拼的告警类型若进库，
    处置侧就查不到它 —— 而告警的价值全在「能处置」。"""
    snapshot = _snapshot(session)
    with pytest.raises(IntegrityError):
        _raw_insert(session, alert_kind="负cap ", snapshot_id=snapshot.id)
    session.rollback()


# ------------------------------------------------------------------ 引用恰好一个来源

def test_alert_with_snapshot_only_is_accepted(session: Session) -> None:
    """全量重算发现的异常（负 cap / 超总格 / 漂移）引用快照。"""
    row = _alert(session, _snapshot(session), alert_kind=AlertKind.DRIFT_EXCEEDED)
    assert row.snapshot_id is not None
    assert row.ledger_txn_id is None


def test_alert_with_ledger_txn_only_is_accepted(session: Session) -> None:
    """事务内增量失败引用台账事务号（16 §11.7：该笔事务整体回滚）。"""
    ledger = _ledger(session)
    row = _alert(
        session,
        _snapshot(session),
        snapshot_id=None,
        ledger_txn_id=ledger.id,
        alert_kind=AlertKind.INCREMENT_FAILED,
        detail="增量写入异常，台账与 cap 已整体回滚",
    )
    assert row.snapshot_id is None
    assert row.ledger_txn_id == ledger.id


def test_alert_rejected_when_both_sources_are_null(session: Session) -> None:
    """spec 场景「CapAlert 引用恰好一个来源」：两个都空 → 拒绝。

    既无快照又无事务的告警无法定位，属于无法处置的脏数据（D5）。
    """
    with pytest.raises(IntegrityError):
        _raw_insert(session, snapshot_id=None, ledger_txn_id=None)
    session.rollback()


def test_alert_rejected_when_both_sources_are_present(session: Session) -> None:
    """两个都非空 → 拒绝：不知道以谁为准（快照是权威 vs 事务是过程，16 §6.4）。

    台账行是真的（`ledger_txn_id` 已是外键），所以这里**必须**命中的是那条 CHECK。
    断言点名约束，是为了与「撞外键」区分开：撞外键说明这一行只是恰好指向了不存在的
    台账，而「恰好一个来源」这条口径本身没被守住。
    """
    snapshot = _snapshot(session)
    ledger = _ledger(session)

    with pytest.raises(IntegrityError) as excinfo:
        _raw_insert(session, snapshot_id=snapshot.id, ledger_txn_id=ledger.id)

    assert "alert_source_exactly_one" in str(excinfo.value)
    session.rollback()


def test_ledger_txn_id_is_now_a_real_foreign_key(session: Session) -> None:
    """§4 补上的外键真的生效：指向不存在的台账被拒（`foreign_keys=ON`，D12）。

    这条是 §3 欠下的账 —— 那一版 `ledger_txn_id` 只是个整数，随便填什么都写得进去。
    """
    with pytest.raises(IntegrityError) as excinfo:
        _raw_insert(session, snapshot_id=None, ledger_txn_id=999_999)

    assert "FOREIGN KEY" in str(excinfo.value).upper()
    session.rollback()


def test_alert_requires_existing_snapshot(session: Session) -> None:
    """引用快照的那一支：指向不存在的快照必须被拒。"""
    with pytest.raises(IntegrityError):
        _raw_insert(session, snapshot_id=999999)
    session.rollback()


def test_alert_aisle_no_must_be_two_chars(session: Session) -> None:
    """巷道号恒为 2 位（= 库位号前 2 位）—— `1` 与 `01` 是不同巷道。"""
    snapshot = _snapshot(session)
    with pytest.raises(IntegrityError):
        _raw_insert(session, snapshot_id=snapshot.id, aisle_no="1")
    session.rollback()


# ------------------------------------------------------------------ 处置与待补的外键

def test_alert_handled_defaults_to_false(session: Session) -> None:
    """「是否已处理」是实体局部值域（D2），默认未处理。

    默认值不是顺手给的：告警写入与处置是两个动作，若默认 `True`，
    运维一刷新就会看到「全部已处理」。
    """
    row = _alert(session, _snapshot(session))
    assert row.handled is False

    row.handled = True
    session.flush()
    session.expire_all()
    assert session.execute(
        text("SELECT handled FROM cap_alerts WHERE id = :i"), {"i": row.id}
    ).scalar_one() in (1, True)


def test_ledger_txn_id_points_at_the_ledger_table() -> None:
    """`ledger_txn_id` 的外键目标 = 台账（D5）。

    §3 写这条用例时断言的是**方向**（「要么没建、要么指向 `ledgers.id`」），因为那一版
    刻意还没建外键。§4 已补上，故收紧成等号 —— 从此这条断言不会因为「外键消失」
    而静默通过。
    """
    column = Base.metadata.tables["cap_alerts"].c.ledger_txn_id
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets == {"ledgers.id"}, f"ledger_txn_id 指向了意外的表：{targets}"
