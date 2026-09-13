"""执行导入的「部分文件失败隔离」端到端（tasks.md 3.3 的 Scenario 落库）。

事实来源：spec `data-import`「部分文件失败隔离」（Scenario：快照失败不阻断 PO/DO 载入）
          openspec/changes/data-import/design.md D4

`execute_import` 是服务层编排（路由在 6.1），故用 `job_api` 夹具直连库。走完整链路
`run_validation → execute_import`（不是直接造 VALIDATED 会话），钉住「校验过了什么，
执行就只载入什么」：

- PO/DO 校验通过、INV 校验失败 → 执行时只载入 PO/DO（JobOrder 两行），INV 被隔离、
  不建 cap 基线（无 Snapshot / AisleCap / InventoryItem），会话停在 `IMPORTED`（无 INV
  就没有 `IMPORTED → BASELINE`）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.core.enums import FileType, ImportStatus, JobType
from app.importer.execute import execute_import
from app.importer.session import SourceFile, run_validation
from app.models.job import JobOrder
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8)
TODAY = datetime(2026, 9, 14).date()

_PODO_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"
PO_CSV = (
    f"{_PODO_HEADER}\n"
    "PO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
)
DO_CSV = (
    f"{_PODO_HEADER}\n"
    "DO-01,20,发货单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,120\n"
)

_INV_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"
BAD_INV_CSV = f"{_INV_HEADER}\nGTJ10036,010104,M1,可乐,,合格,40,2026-09-08\n"  # 批号为空


def _csv(file_type: FileType, filename: str, text: str) -> SourceFile:
    return SourceFile(file_type=file_type, filename=filename, content=text.encode("utf-8"))


def _new_draft_session(db) -> ImportSession:
    row = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=NOW,
    )
    db.add(row)
    db.flush()
    return row


def _read(factory) -> tuple[tuple[JobOrder, ...], tuple[Snapshot, ...], tuple[InventoryItem, ...], tuple[AisleCap, ...]]:
    with factory() as db:
        return (
            tuple(db.scalars(select(JobOrder).order_by(JobOrder.id))),
            tuple(db.scalars(select(Snapshot).order_by(Snapshot.id))),
            tuple(db.scalars(select(InventoryItem).order_by(InventoryItem.id))),
            tuple(db.scalars(select(AisleCap).order_by(AisleCap.aisle_no))),
        )


def test_inv_failure_does_not_block_po_do_and_skips_baseline(job_api) -> None:
    """PO/DO 通过 + INV 失败 → 只载入 PO/DO、不建基线，会话停在 IMPORTED。"""
    files = [
        _csv(FileType.PO, "PO.csv", PO_CSV),
        _csv(FileType.DO, "DO.csv", DO_CSV),
        _csv(FileType.INV, "INV.csv", BAD_INV_CSV),
    ]

    with job_api.factory() as db:
        session_row = _new_draft_session(db)
        run_validation(db, session_row, files, now=TODAY)
        assert session_row.status is ImportStatus.VALIDATED
        assert session_row.receipt_json["success_files"] == 2
        assert session_row.receipt_json["failed_files"] == 1

        execute_import(db, session_row, files)
        assert session_row.status is ImportStatus.IMPORTED  # 无 INV → 不越到 BASELINE
        assert session_row.baselined_at is None
        db.commit()

    orders, snapshots, items, caps = _read(job_api.factory)
    # PO/DO 各去各自队列/任务；INV 被隔离 → 无库存、无快照、无 cap。
    assert [o.job_type for o in orders] == [JobType.INBOUND, JobType.OUTBOUND]
    assert [o.status.value for o in orders] == ["PENDING", "PENDING"]
    assert snapshots == () and items == () and caps == ()
