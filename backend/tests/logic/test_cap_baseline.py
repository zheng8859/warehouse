"""cap 基线全量重算（`aggregate_aisle_caps` / `establish_baseline`）的四项指标与版本归档测试（tasks.md 4.2）。

事实来源：16-数据衔接与 cap 自维护设计 §6.1（cap 四项口径）/ §6.2（基线建立）
          17-数据模型设计 §3.4（AisleCap）、§3.2（Snapshot 追加式）、§10.5（cap 快照 JSON）
          openspec/changes/data-import/design.md D5（cap_physical 口径 + cap_reserved =
          cap_physical × 40% 固定预留带，仅近站台巷道）
          spec `data-import`「cap 基线全量重算」（重算失败回滚该批并提示重导）

钉住四条口径：

  1. **cap_physical** = 巷道内**去重库位格数**（主数据到位前 = 快照库位去重格数近似，D5）。
  2. **cap_total = cap_physical − 已占格数**。已占格数本 change 以「有库存的库位去重格数」
     近似（design.md 风险表；板-格换算 D14 确认后单列修正）。因快照里每个库位 qty > 0，
     「去重格数」与「有库存去重格数」相等 → 本阶段 cap_total 恒为 0（已知占位，见
     `aggregate_aisle_caps` docstring）。
  3. **cap_reserved = cap_physical × 40%**（固定预留带，**仅近站台巷道非零**；
     `is_near_station is None` = 未导出 → 按非近站台，reserved = 0，且落 NULL 不当 False）。
  4. **cap_usable = max(cap_total − cap_reserved, 0)**。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cap.baseline import aggregate_aisle_caps, establish_baseline
from app.core.enums import ImportStatus
from app.core.errors import StateConflict
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 0, 0)


def _item(
    location_code: str,
    *,
    material_code: str = "M1",
    batch_no: str = "B1",
    qty: int = 40,
) -> InventoryItem:
    """一行待挂到新快照下的库存（`snapshot_id` 由 `establish_baseline` 回填，故不传）。"""
    return InventoryItem(
        warehouse_id=WAREHOUSE,
        location_code=location_code,
        material_code=material_code,
        batch_no=batch_no,
        item_status="合格",
        qty=qty,
        snapshot_time=NOW,
    )


def _imported_session(session: Session, idx: int = 1) -> ImportSession:
    """一条 `IMPORTED` 会话（`establish_baseline` 的入口状态）。"""
    row = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no=f"IMP-20260908-{idx:02d}",
        import_batch_no=f"BAT-20260908-{idx:02d}",
        data_time=NOW,
        status=ImportStatus.IMPORTED,
    )
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------ 纯函数：四项指标

def test_aggregate_four_metrics_per_aisle() -> None:
    """按巷道（`[:2]`）聚合：物理格数 / 总格 / 预留 / 可用，各按口径算出。

    巷道 01 三个去重库位且近站台：reserved = round(3 × 40%) = 1；
    巷道 02 两个去重库位、非近站台：reserved = 0。
    """
    items = [
        _item("010104"), _item("010205"), _item("010206"),  # 巷道 01：3 个库位
        _item("020101"), _item("020102"),                    # 巷道 02：2 个库位
    ]
    caps = aggregate_aisle_caps(
        items,
        warehouse_id=WAREHOUSE,
        snapshot_id=1,
        reserve_ratio=0.40,
        is_near_station={"01": True, "02": False},
    )
    by_aisle = {c.aisle_no: c for c in caps}

    assert set(by_aisle) == {"01", "02"}
    assert by_aisle["01"].cap_physical == 3
    assert by_aisle["01"].cap_total == 0
    assert by_aisle["01"].cap_reserved == 1  # round(3 × 0.40) = 1
    assert by_aisle["01"].cap_usable == 0
    assert by_aisle["02"].cap_physical == 2
    assert by_aisle["02"].cap_reserved == 0  # 非近站台 → 无预留带
    assert by_aisle["02"].is_near_station is False


def test_aggregate_dedups_locations_not_rows() -> None:
    """cap_physical 数的是**去重库位**，不是库存行数 —— 同库位多批/多料只算一格。"""
    items = [
        _item("010104", material_code="M1", batch_no="B1"),
        _item("010104", material_code="M2", batch_no="B2"),  # 同库位、不同料号
    ]
    caps = aggregate_aisle_caps(items, warehouse_id=WAREHOUSE, snapshot_id=1)
    by_aisle = {c.aisle_no: c for c in caps}
    assert by_aisle["01"].cap_physical == 1


def test_aggregate_unknown_near_station_reserves_zero_and_keeps_null() -> None:
    """`is_near_station is None`（未导出）→ reserved = 0，且列落 NULL 不当 `False` 用。"""
    items = [_item("010104"), _item("010205")]
    caps = aggregate_aisle_caps(
        items, warehouse_id=WAREHOUSE, snapshot_id=1, is_near_station={}
    )
    by_aisle = {c.aisle_no: c for c in caps}
    assert by_aisle["01"].cap_reserved == 0
    assert by_aisle["01"].is_near_station is None


# ------------------------------------------------------------------ 编排：版本与归档

def test_establish_baseline_creates_snapshot_and_caps(session: Session) -> None:
    """IMPORTED → BASELINE：生成新快照版本、挂库存行、落 AisleCap、回写会话三列。"""
    row = _imported_session(session)
    snapshot = establish_baseline(
        session,
        import_session=row,
        items=[_item("010104"), _item("010205"), _item("020101")],
        is_near_station={"01": True},
    )

    assert snapshot.version_no == 1
    assert row.status is ImportStatus.BASELINE
    assert row.snapshot_version_no == 1
    assert row.baselined_at is not None

    db_items = session.scalars(
        select(InventoryItem).where(InventoryItem.snapshot_id == snapshot.id)
    ).all()
    assert {i.location_code for i in db_items} == {"010104", "010205", "020101"}

    caps = session.scalars(
        select(AisleCap).where(AisleCap.snapshot_id == snapshot.id)
    ).all()
    assert {c.aisle_no for c in caps} == {"01", "02"}
    assert snapshot.cap_snapshot_json["aisles"]  # 快照 JSON 与行同事务落库


def test_establish_baseline_archives_old_version(session: Session) -> None:
    """新快照生成新版本，旧版本**归档不删除**（追加式，16 §6.2 / 17 §3.2）。"""
    first = _imported_session(session, idx=1)
    snap1 = establish_baseline(
        session, import_session=first, items=[_item("010104")], is_near_station={}
    )
    second = _imported_session(session, idx=2)
    snap2 = establish_baseline(
        session, import_session=second, items=[_item("020101")], is_near_station={}
    )

    assert (snap1.version_no, snap2.version_no) == (1, 2)
    versions = {s.version_no for s in session.scalars(select(Snapshot))}
    assert versions == {1, 2}


def test_establish_baseline_requires_imported(session: Session) -> None:
    """入口非 `IMPORTED`（如 `DRAFT`）→ `StateConflict`，零写入。"""
    row = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=NOW,
        status=ImportStatus.DRAFT,
    )
    session.add(row)
    session.flush()

    with pytest.raises(StateConflict):
        establish_baseline(session, import_session=row, items=[_item("010104")])
    assert session.scalars(select(Snapshot)).all() == []


def test_establish_baseline_failure_rolls_back_whole_batch(session: Session) -> None:
    """重算失败（坏库位号撞 CHECK）→ 整体回滚：无新快照 / 无 AisleCap，会话回到 `IMPORTED`。

    库位号非 6 位（`01010`）违反 `InventoryItem` 的 `length(location_code)=6` CHECK，
    在 savepoint 内 flush 时抛 `IntegrityError` —— 快照、库存行、cap 行一并回滚，
    不产生半成品基线（spec「重算异常回滚并提示重导」）。会话停在 `IMPORTED`：状态机
    没有 `IMPORTED → FAILED` 的边，失败由调用方据此提示重导。
    """
    row = _imported_session(session)

    with pytest.raises(IntegrityError):
        establish_baseline(
            session, import_session=row, items=[_item("01010")], is_near_station={}
        )

    assert session.scalars(select(Snapshot)).all() == []
    assert session.scalars(select(AisleCap)).all() == []
    assert row.status is ImportStatus.IMPORTED
