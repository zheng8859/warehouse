"""cap 基线全量重算（`aggregate_aisle_caps` / `establish_baseline`）的四项指标与版本归档测试（tasks.md 4.2）。

事实来源：16-数据衔接与 cap 自维护设计 §6.1（cap 四项口径）/ §6.2（基线建立）
          17-数据模型设计 §3.4（AisleCap）、§3.2（Snapshot 追加式）、§10.5（cap 快照 JSON）
          openspec/changes/data-import/design.md D5（cap_physical 口径 + cap_reserved =
          cap_physical × 40% 固定预留带，仅近站台巷道）
          spec `data-import`「cap 基线全量重算」（重算失败回滚该批并提示重导）

钉住四条口径：

  1. **cap_physical** = 巷道物理总格数，取主数据 `Aisle.total_cells`；主数据缺失时
     退化为「快照去重库位格数」（D5 近似）。
  2. **cap_total = cap_physical − 已占格数**。D14 已确认 **1 板 = 1 格**（2026-09-15
     业务确认）：快照粒度为库位级，已占格数 = 有库存的去重库位数（同库位多批/多料只
     算 1 格），不按 qty 折算。主数据到位时 cap_total 为真实剩余容量；主数据缺失退化
     时 physical 与 occupied 相等 → cap_total = 0（「主数据未到位」信号，非真实容量）。
     主数据里零库存的空巷也出行（occupied=0），否则进不了引擎「主数据 ∩ cap 行」可
     行集。
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

from app.cap.baseline import (
    aggregate_aisle_caps,
    establish_baseline,
    recompute_snapshot_caps,
)
from app.core.enums import ImportStatus
from app.core.errors import StateConflict
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot
from app.models.master_data import Aisle

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


def test_aggregate_physical_from_master_gives_real_remaining() -> None:
    """D14：主数据给 total_cells 时，cap_total = 总格数 − 去重占用库位（不按 qty 折算）。

    巷道 01：主数据 10 格、库存占 3 个去重库位（其中一个库位混料两行仍只算 1 格），
    近站台 → total=7、reserved=round(10×40%)=4、usable=3。
    """
    items = [
        _item("010104", qty=102),
        _item("010205", qty=5),                  # 零头箱数不影响格数
        _item("010206", material_code="M2"),    # 同库位不同料 → 仍只占 1 格
        _item("010206", material_code="M3"),
    ]
    caps = aggregate_aisle_caps(
        items,
        warehouse_id=WAREHOUSE,
        snapshot_id=1,
        is_near_station={"01": True},
        physical_cells={"01": 10},
    )
    cap = {c.aisle_no: c for c in caps}["01"]
    assert cap.cap_physical == 10
    assert cap.cap_total == 7          # 10 − 3 个去重库位
    assert cap.cap_reserved == 4       # round(10 × 0.40) = 4
    assert cap.cap_usable == 3         # 7 − 4


def test_aggregate_emits_empty_master_aisles_with_zero_occupied() -> None:
    """主数据里有、快照零库存的空巷必须出行（occupied=0），否则进不了入库候选集。

    引擎可行集 = 主数据 ∩ cap 行（scoring.feasible_aisles）：空巷不出库就永远不会被
    推荐入库。03 无库存但主数据给 200 格 → physical=200/total=200/reserved 按近站台算。
    """
    items = [_item("010104")]
    caps = aggregate_aisle_caps(
        items,
        warehouse_id=WAREHOUSE,
        snapshot_id=1,
        is_near_station={"01": False, "03": True},
        physical_cells={"01": 100, "03": 200},
    )
    by_aisle = {c.aisle_no: c for c in caps}
    assert set(by_aisle) == {"01", "03"}
    empty = by_aisle["03"]
    assert empty.cap_physical == 200
    assert empty.cap_total == 200
    assert empty.cap_reserved == 80    # 空的近站台巷道仍保留预留带
    assert empty.cap_usable == 120
    assert empty.is_near_station is True


def test_aggregate_missing_master_falls_back_to_distinct_locations() -> None:
    """主数据缺该巷（缺键 / None / 非正）→ 退化 physical=去重库位，cap_total=0（D5）。"""
    items = [_item("010104"), _item("010205")]
    caps = aggregate_aisle_caps(
        items,
        warehouse_id=WAREHOUSE,
        snapshot_id=1,
        is_near_station={"01": True},
        physical_cells={"01": None},
    )
    cap = {c.aisle_no: c for c in caps}["01"]
    assert cap.cap_physical == 2
    assert cap.cap_total == 0


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


def test_establish_baseline_reads_aisle_master(session: Session) -> None:
    """生产路径：execute 不传映射时，physical / near 全部从 `Aisle` 主数据读。

    主数据含一条本快照零库存的空巷 03 → cap 行必须出现，且 01/02 的 cap_total 按
    权威总格数算出真实剩余（不再恒 0）。
    """
    session.add_all(
        [
            Aisle(warehouse_id=WAREHOUSE, aisle_no="01", total_cells=100,
                  is_near_station=True),
            Aisle(warehouse_id=WAREHOUSE, aisle_no="02", total_cells=200,
                  is_near_station=False),
            Aisle(warehouse_id=WAREHOUSE, aisle_no="03", total_cells=200,
                  is_near_station=False),
        ]
    )
    session.flush()
    row = _imported_session(session)
    snapshot = establish_baseline(
        session,
        import_session=row,
        items=[_item("010104"), _item("010205"), _item("020101")],
    )

    caps = {
        c.aisle_no: c
        for c in session.scalars(
            select(AisleCap).where(AisleCap.snapshot_id == snapshot.id)
        )
    }
    assert set(caps) == {"01", "02", "03"}          # 空巷 03 也出行
    assert caps["01"].cap_physical == 100
    assert caps["01"].cap_total == 98               # 100 − 2 占用
    assert caps["01"].cap_reserved == 40            # 近站台 round(100×40%)
    assert caps["01"].is_near_station is True
    assert caps["02"].cap_physical == 200
    assert caps["02"].cap_total == 199
    assert caps["03"].cap_total == 200              # 零库存空巷


def test_recompute_refreshes_caps_from_latest_master(session: Session) -> None:
    """主数据补齐后对旧快照重算：physical/near 以当前主数据为准，不产生新版本。

    先在**无主数据**下建基线（退化 cap_total=0），再补 Aisle 主数据（含空巷 03），
    `recompute_snapshot_caps` 后旧快照 cap 拉到权威口径，near 三态也被主数据刷新。
    """
    row = _imported_session(session)
    snapshot = establish_baseline(
        session,
        import_session=row,
        items=[_item("010104"), _item("010205")],
        is_near_station={},
        physical_cells={},
    )
    before = {
        c.aisle_no: c
        for c in session.scalars(select(AisleCap).where(AisleCap.snapshot_id == snapshot.id))
    }
    assert before["01"].cap_total == 0
    assert before["01"].is_near_station is None

    session.add_all(
        [
            Aisle(warehouse_id=WAREHOUSE, aisle_no="01", total_cells=100,
                  is_near_station=True),
            Aisle(warehouse_id=WAREHOUSE, aisle_no="03", total_cells=200,
                  is_near_station=False),
        ]
    )
    session.flush()

    recompute_snapshot_caps(session, snapshot=snapshot)
    session.expire_all()
    after = {
        c.aisle_no: c
        for c in session.scalars(select(AisleCap).where(AisleCap.snapshot_id == snapshot.id))
    }
    assert set(after) == {"01", "03"}               # 空巷 03 补出行
    assert after["01"].cap_physical == 100
    assert after["01"].cap_total == 98
    assert after["01"].cap_reserved == 40
    assert after["01"].is_near_station is True
    assert after["03"].cap_total == 200
    assert session.scalars(select(Snapshot)).all() == [snapshot]  # 仍是同一快照
