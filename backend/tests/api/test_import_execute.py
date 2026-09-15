"""执行导入分流 PO/DO → JobOrder（tasks.md 3.1 的验证）。

事实来源：16-数据衔接与 cap 自维护 §3（VALIDATED→IMPORTING→IMPORTED/FAILED）· §4.4
          （执行导入分流）· A.4（PO/DO 分流只做两件事，PENDING 初值）
          spec `data-import`「建立基准（执行导入分流）」（写入失败回滚该批）
          openspec/changes/data-import/design.md D4

`execute_import` 是服务层编排（非 HTTP 端点 —— 路由在 tasks.md 6.1），故用 `job_api`
夹具的 `factory` 直连库、直接调函数，不经过 TestClient。要钉住三点：

1. **载入**：PO → `JobOrder(job_type=INBOUND)`、DO → `JobOrder(job_type=OUTBOUND)`，
   业务状态初值 `PENDING`，字段取自源行（单号/行号/料号/品名/数量）。PO 在入库单建立
   时生成批号（D11，注入固定 `now` 钉住）；DO 的 `batch_no` 留空。
2. **回滚**：同一 PO 内重复（单号+行号）撞唯一键 → 整批回滚，不产出半成品，退回 FAILED。
3. **守卫**：入口非 `VALIDATED` 抛 `StateConflict`（try 之外，不是「执行失败」）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.core.enums import FileType, ImportStatus, JobStatus, JobType
from app.core.errors import StateConflict
from app.importer.execute import execute_import
from app.importer.session import SourceFile
from app.models.linkage import ImportSession

WAREHOUSE = "GTJ10036"

# PO/DO 同构（16 A.2 / A.3）：单据号码/行号/类型/仓库号/料号/品名/生产日期/数量。
_HEADER = "单据号码,行号,类型,仓库号,料号,品名,生产日期,数量"

PO_CSV = (
    f"{_HEADER}\n"
    "PO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
)
DO_CSV = (
    f"{_HEADER}\n"
    "DO-01,20,发货单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,120\n"
)
# 同一 PO 内 (order_no, line_no) 撞唯一键 —— 用于钉住「写入失败回滚该批」。
DUP_PO_CSV = (
    f"{_HEADER}\n"
    "PO-01,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
    "PO-01,10,生产订单,GTJ10036,3005678,PET500 茉莉柚茶,2026-09-05,300\n"
)


def _csv(file_type: FileType, filename: str, text: str) -> SourceFile:
    return SourceFile(file_type=file_type, filename=filename, content=text.encode("utf-8"))


def _validated_session(db) -> ImportSession:
    """构造一个已通过校验（VALIDATED）、可直接执行的会话。"""
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


# ------------------------------------------------------------------ 载入：PO/DO → JobOrder

def test_po_splits_to_inbound_pending_order(job_api) -> None:
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(
            db, session_row, [_csv(FileType.PO, "PO.csv", PO_CSV)], now=datetime(2026, 9, 8)
        )
        db.commit()

    orders = job_api.orders()
    assert len(orders) == 1
    order = orders[0]
    assert order.job_type is JobType.INBOUND
    assert order.status is JobStatus.PENDING
    assert order.order_no == "PO-01"
    assert order.line_no == "10"
    assert order.material_code == "3001234"
    assert order.material_name == "PET500 茉莉柚茶"
    assert order.qty == 500
    # 批号由入库单建立时按生产批规则生成（D11），不再留空：2026-09-08 → GJP2690871。
    assert order.batch_no == "GJP2690871"


def test_do_splits_to_outbound_pending_order(job_api) -> None:
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.DO, "DO.csv", DO_CSV)])
        db.commit()

    orders = job_api.orders()
    assert len(orders) == 1
    assert orders[0].job_type is JobType.OUTBOUND
    assert orders[0].status is JobStatus.PENDING
    assert orders[0].order_no == "DO-01"
    assert orders[0].qty == 120
    assert orders[0].batch_no is None  # 出库单不生成批号，顺路取时才从库存明细选出


def test_po_and_do_together_load_both_queues(job_api) -> None:
    """一份会话同载 PO 与 DO：`job_type` 各自派生，互不干扰。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(
            db,
            session_row,
            [_csv(FileType.PO, "PO.csv", PO_CSV), _csv(FileType.DO, "DO.csv", DO_CSV)],
        )
        assert session_row.status is ImportStatus.IMPORTED
        assert session_row.imported_at is not None
        db.commit()

    orders = job_api.orders()
    assert [o.job_type for o in orders] == [JobType.INBOUND, JobType.OUTBOUND]


def test_multi_line_po_produces_one_order_per_line(job_api) -> None:
    """一行 PO → 一张 JobOrder（行唯一键 = 单号 + 行号，多行各占一张单）。"""
    multi = (
        f"{_HEADER}\n"
        "PO-02,10,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
        "PO-02,20,生产订单,GTJ10036,3001234,PET500 茉莉柚茶,2026-09-05,500\n"
    )
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.PO, "PO.csv", multi)])
        db.commit()

    orders = job_api.orders()
    assert len(orders) == 2
    assert {o.line_no for o in orders} == {"10", "20"}


# ------------------------------------------------------------------ 回滚：写入失败整批回退

def test_write_failure_rolls_back_whole_batch(job_api) -> None:
    """重复（单号+行号）撞唯一键 → 整批回滚，不产出半成品，会话退回 FAILED。"""
    with job_api.factory() as db:
        session_row = _validated_session(db)
        execute_import(db, session_row, [_csv(FileType.PO, "PO.csv", DUP_PO_CSV)])
        assert session_row.status is ImportStatus.FAILED
        assert session_row.imported_at is None
        db.commit()

    assert job_api.orders() == ()  # 无半成品 JobOrder 落库


def test_execute_requires_validated_entry(job_api) -> None:
    """入口非 VALIDATED（如 DRAFT）→ StateConflict，状态不动、不写单。"""
    with job_api.factory() as db:
        row = ImportSession(
            warehouse_id=WAREHOUSE,
            session_no="IMP-20260908-02",
            import_batch_no="BAT-20260908-02",
            data_time=datetime(2026, 9, 8),
            status=ImportStatus.DRAFT,
        )
        db.add(row)
        db.flush()

        with pytest.raises(StateConflict):
            execute_import(db, row, [_csv(FileType.PO, "PO.csv", PO_CSV)])
        db.commit()

    assert job_api.orders() == ()
