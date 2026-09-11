"""`CapAlert` 的取值域与「引用恰好一个来源」契约（tasks 3.5 的验证）。

事实来源：17-数据模型设计 §3.4 · 16-数据衔接与 cap 自维护 §6.5 / §11.7
          openspec/changes/data-model-permission/design.md D2、D5
          spec `data-model`「不可变快照基线与 cap 引用」场景「CapAlert 引用恰好一个来源」

**为什么这条进 `tests/logic/` 而不是 `tests/models/`**：`alert_kind` 与「恰好一个来源」
不是单纯的 CRUD 约束，而是 16 §6.5 四类 cap 异常在本模型里的落点 —— 负 cap / 超总格 /
漂移超阈值由**快照重算**发现（引用快照），增量失败由**台账事务**发现（引用事务号）。
两个都空 = 无法定位的告警，两个都非空 = 不知道该以谁为准（D5）。三层 TDD 的分工里
这类「口径判定」归逻辑层（00-总体开发方案 §3.1）。

**一处本阶段无法完全验到的**：`ledger_txn_id` 此刻**没有外键**（`ledgers` 表属作业链 §4，
见 `app/models/linkage.py` 模块 docstring 第 4 条）。§4 补上外键后，
下文的「两个都非空」用例会先撞外键而不是 CHECK —— 届时该用例需要一个真实的台账行做夹具，
断言也应从「抛 IntegrityError」收紧为「抛的是 CHECK 不是外键」。已登记在 tasks.md 9.4b。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.enums import ImportStatus
from app.models.base import Base
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
    row = _alert(
        session,
        _snapshot(session),
        snapshot_id=None,
        ledger_txn_id=42,
        alert_kind=AlertKind.INCREMENT_FAILED,
        detail="增量写入异常，台账与 cap 已整体回滚",
    )
    assert row.snapshot_id is None
    assert row.ledger_txn_id == 42


def test_alert_rejected_when_both_sources_are_null(session: Session) -> None:
    """spec 场景「CapAlert 引用恰好一个来源」：两个都空 → 拒绝。

    既无快照又无事务的告警无法定位，属于无法处置的脏数据（D5）。
    """
    with pytest.raises(IntegrityError):
        _raw_insert(session, snapshot_id=None, ledger_txn_id=None)
    session.rollback()


def test_alert_rejected_when_both_sources_are_present(session: Session) -> None:
    """两个都非空 → 拒绝：不知道以谁为准（快照是权威 vs 事务是过程，16 §6.4）。

    ⚠️ §4 给 `ledger_txn_id` 补上外键后，这条用例需要先造一行真实台账，
    否则撞到的是外键而非 CHECK（见模块 docstring 末尾）。
    """
    snapshot = _snapshot(session)
    with pytest.raises(IntegrityError):
        _raw_insert(session, snapshot_id=snapshot.id, ledger_txn_id=42)
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


def test_ledger_txn_id_never_points_elsewhere() -> None:
    """`ledger_txn_id` 本阶段无外键（D5 要求它是外键，目标表 `ledgers` 属 §4）。

    这里不做「永远无外键」的断言 —— 那会在 §4 补外键时变成假失败。断言的是**方向**：
    它要么还没建外键，要么指向 `ledgers.id`，绝不会指向别处。
    """
    column = Base.metadata.tables["cap_alerts"].c.ledger_txn_id
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets <= {"ledgers.id"}, f"ledger_txn_id 指向了意外的表：{targets}"
