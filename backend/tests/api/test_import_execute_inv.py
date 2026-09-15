"""执行导入分流 INV → InventoryItem + 触发 cap 基线（tasks.md 3.2 的验证）。

事实来源：16-数据衔接与 cap 自维护 §3（IMPORTED→BASELINE）· §4.4（执行导入分流）· A.1/A.4
          spec `data-import`「执行导入与建立基准」（Scenario：三类文件分流到各自去向）
          spec `data-import`「cap 基线全量重算」（快照导入触发全量重算与新版本归档）
          openspec/changes/data-import/design.md D4 / D5

`execute_import` 是服务层编排（非 HTTP 端点 —— 路由在 tasks.md 6.1），故用 `job_api`
夹具直连库、直接调函数（同 `test_import_execute.py`）。要钉住三点：

1. **分流**：INV → `InventoryItem`（库位号 6 位文本 / 数量整数 / 批号 / 状态 /
   库存记录时间），`snapshot_id` 由基线回填。
2. **基线触发**：有 INV 时 `IMPORTED → BASELINE` —— 生成 `Snapshot`（版本 1）+ 每巷道
   `AisleCap`（is_near_station 未导出 → NULL、reserved=0），会话落 `baselined_at`。
3. **三类同载**：PO/DO → JobOrder、INV → InventoryItem + 基线，各去各自去向。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.core.enums import FileType, ImportStatus, JobType
from app.importer.execute import execute_import
from app.importer.session import SourceFile
from app.models.job import JobOrder
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

WAREHOUSE = "GTJ10036"

# INV 8 列（16 A.1）：仓库号/库位号/料号/品名/批号/状态/数量/库存记录时间。
_INV_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"

# 巷道 01 两个去重库位、巷道 02 一个去重库位 —— 应产出 3 行库存 + 2 行 AisleCap。
INV_CSV = (
    f"{_INV_HEADER}\n"
    "GTJ10036,010104,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,010205,3001234,PET500 茉莉柚茶,B001,合格,40,2026-09-08 00:00:00\n"
    "GTJ10036,020101,3005678,PET500 茉莉柚茶,B002,合格,40,2026-09-08 00:00:00\n"
)

# PO/DO 同构（16 A.2/A.3），与 test_import_execute.py 同一头。
_PODO_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"
PO_CSV = (
    f"{_PODO_HEADER}\n"
    "PO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
)
DO_CSV = (
    f"{_PODO_HEADER}\n"
    "DO-01,20,发货单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,120\n"
)


def _csv(file_type: FileType, filename: str, text: str) -> SourceFile:
    return SourceFile(file_type=file_type, filename=filename, content=text.encode("utf-8"))


def _validated_session(db) -> ImportSession:
    row = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=datetime(2026, 9, 8),
        status=ImportStatus.VALIDATED,
    )
    db.add(row)
    db.flush()
    return row


def _read(factory) -> tuple[tuple[InventoryItem, ...], tuple[Snapshot, ...], tuple[AisleCap, ...], tuple[JobOrder, ...]]:
    """读回四类行（`expire_on_commit=False`，会话关后列属性仍可读，同 `job_api.orders`）。"""
    with factory() as db:
        return (
            tuple(db.scalars(select(InventoryItem).order_by(InventoryItem.id))),
            tuple(db.scalars(select(Snapshot).order_by(Snapshot.id))),
            tuple(db.scalars(select(AisleCap).order_by(AisleCap.aisle_no))),
            tuple(db.scalars(select(JobOrder).order_by(JobOrder.id))),
        )


# ------------------------------------------------------------------ 分流：INV → InventoryItem

def test_inv_splits_to_inventory_and_establishes_baseline(job_api) -> None:
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.INV, "INV.csv", INV_CSV)])
        assert session_row.status is ImportStatus.BASELINE
        assert session_row.imported_at is not None
        assert session_row.baselined_at is not None
        assert session_row.snapshot_version_no == 1
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    # 快照基线：一个版本、一个 cap 快照 JSON。
    assert len(snapshots) == 1
    assert snapshots[0].version_no == 1
    assert snapshots[0].snapshot_time == datetime(2026, 9, 8)
    assert snapshots[0].cap_snapshot_json["aisles"]

    # 库存分布：3 行，字段取自源行，snapshot_id 指向新快照。
    assert len(items) == 3
    assert {i.location_code for i in items} == {"010104", "010205", "020101"}
    first = next(i for i in items if i.location_code == "010104")
    assert first.material_code == "3001234"
    assert first.batch_no == "B001"
    assert first.item_status == "合格"
    assert first.qty == 40
    assert first.snapshot_time == datetime(2026, 9, 8)
    assert {i.snapshot_id for i in items} == {snapshots[0].id}

    # cap 基线：每巷道一行；主数据未导出 → is_near_station NULL、reserved 0。
    assert {c.aisle_no for c in caps} == {"01", "02"}
    assert {c.cap_physical for c in caps} == {2, 1}
    assert all(c.is_near_station is None for c in caps)
    assert all(c.cap_reserved == 0 for c in caps)

    # 无 PO/DO → 无 JobOrder。
    assert orders == ()


def test_inv_only_establishes_baseline_without_orders(job_api) -> None:
    """只载 INV：建基线、不产 JobOrder（快照不生成队列/任务）。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.INV, "INV.csv", INV_CSV)])
        assert session_row.status is ImportStatus.BASELINE
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    assert len(snapshots) == 1 and len(caps) == 2 and len(items) == 3
    assert orders == ()


# ------------------------------------------------------------------ 三类同载

def test_mixed_files_split_to_all_destinations(job_api) -> None:
    """PO + DO + INV 同载：队列/任务 + 库存 + 基线各去各自去向，会话终态 BASELINE。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(
            db,
            session_row,
            [
                _csv(FileType.PO, "PO.csv", PO_CSV),
                _csv(FileType.DO, "DO.csv", DO_CSV),
                _csv(FileType.INV, "INV.csv", INV_CSV),
            ],
        )
        assert session_row.status is ImportStatus.BASELINE
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    assert len(snapshots) == 1 and len(items) == 3 and len(caps) == 2
    assert [o.job_type for o in orders] == [JobType.INBOUND, JobType.OUTBOUND]
    assert [o.status.value for o in orders] == ["PENDING", "PENDING"]


def test_po_do_without_inv_stays_imported(job_api) -> None:
    """无 INV 时不建基线：会话停在 IMPORTED（不越到 BASELINE —— 快照缺失）。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.PO, "PO.csv", PO_CSV)])
        assert session_row.status is ImportStatus.IMPORTED
        assert session_row.baselined_at is None
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    assert snapshots == () and caps == () and items == ()
    assert len(orders) == 1


# ------------------------------------------------------------------ 库存记录时间格式（导入页 500 回归）

# WMS 真实导出形态：时间是无分隔符 8 位 `20260915`，状态用「良品」。
INV_CSV_COMPACT_TIME = (
    f"{_INV_HEADER}\n"
    "GTJ10036,010104,3001234,PET500 茉莉柚茶,B001,良品,40,20260915\n"
    "GTJ10036,020101,3005678,PET500 茉莉柚茶,B002,良品,40,20260915\n"
)

# 完全无法解析的时间（正常校验层会先拦住；此处验证执行层的纵深防御）。
INV_CSV_BAD_TIME = (
    f"{_INV_HEADER}\n"
    "GTJ10036,010104,3001234,PET500 茉莉柚茶,B001,良品,40,二零二六年\n"
)


def test_inv_compact_yyyymmdd_snapshot_time_baselines(job_api) -> None:
    """无分隔符 `20260915` 能正常分流并建基线（本次导入页 500 的真实数据形态）。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.INV, "INV.csv", INV_CSV_COMPACT_TIME)])
        assert session_row.status is ImportStatus.BASELINE
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    assert len(snapshots) == 1 and len(items) == 2 and len(caps) == 2
    assert all(i.snapshot_time == datetime(2026, 9, 15) for i in items)
    assert orders == ()


def test_inv_unparseable_snapshot_time_fails_without_raising(job_api) -> None:
    """执行期遇到无法解析的时间：落 FAILED 并返回，**不抛异常**（不得冒泡成 HTTP 500），
    且不产生任何快照 / 库存 / cap 半成品。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        result = execute_import(db, session_row, [_csv(FileType.INV, "INV.csv", INV_CSV_BAD_TIME)])
        assert result.status is ImportStatus.FAILED
        db.commit()

    items, snapshots, caps, orders = _read(job_api.factory)
    assert snapshots == () and items == () and caps == () and orders == ()
