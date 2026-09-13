"""执行导入分流：PO/DO → JobOrder、INV → InventoryItem + cap 基线（**编排层：做 IO**）。

事实来源：16-数据衔接与 cap 自维护 §3（VALIDATED→IMPORTING→IMPORTED/FAILED→BASELINE）
          · §4.4（执行导入分流）· A.4（PO/DO 分流只做两件事，PENDING 初值）
          spec `data-import`「建立基准（执行导入分流）」（写入失败回滚该批）
          spec `data-import`「cap 基线全量重算」（快照导入触发全量重算与新版本归档）
          openspec/changes/data-import/design.md D4 / D5
          CLAUDE.md §四（写并发由乐观锁版本号保证）

## 职责边界

`execute_import` 与 `session.run_validation` 对称：后者走 `DRAFT → VALIDATING →
VALIDATED|FAILED`，前者走 `VALIDATED → IMPORTING → IMPORTED → [BASELINE]`。三类文件
各去各自去向（16 A.4 / spec「执行导入与建立基准」）：

1. PO → `JobOrder(job_type=INBOUND)`（入库待推荐队列）、DO → `JobOrder(job_type=OUTBOUND)`
   （出库拣配任务），业务状态初值 `PENDING`。行唯一键 `(warehouse_id, job_type, order_no,
   line_no)` 即 `JobOrder` 联合唯一约束，重复导入撞它即被拦下。
2. INV → `InventoryItem`（既有库位/批次分布），随后触发 `establish_baseline` 建 cap 基线
   （`IMPORTED → BASELINE`）：生成新 `Snapshot` 版本 + 每巷道 `AisleCap`。

**不触发** ABC 派生 —— 那是脚本通道（任务 5.1）的事，且不进本状态机。

## 事务与回滚

沿用 `confirm.py` 的 savepoint 手法：`VALIDATED → IMPORTING` 的守卫与 flush 在 try 之外
（初始状态不对是「调用方过期」，不是「执行失败」）。「载入 JobOrder → `IMPORTING →
IMPORTED`」包在 `begin_nested()` 里，任一步失败整体回滚，再走 `IMPORTING → FAILED` 回边。

INV 的 `InventoryItem` **不在此 savepoint 内 flush**：其 `snapshot_id` 是到 `snapshots`
的外键且 NOT NULL，快照还没建就 flush 必撞约束。故分流只产出对象，真正落库交给
`establish_baseline`（它先建快照、回填 `snapshot_id`、再 flush，自己还有一个 savepoint）。
基线重算失败原样上抛（状态机没有 `IMPORTED → FAILED` 回边），由调用方整体回滚该批并
提示「重算异常，请修正后重导」。不 commit —— 事务边界属于 API 路由。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cap.baseline import establish_baseline
from app.core.enums import FileType, ImportStatus, JobType
from app.core.errors import DomainError
from app.core.import_state import assert_import_transition
from app.importer.loaders import as_location_code
from app.importer.mapping import MappingResult
from app.importer.session import SourceFile, parse_source
from app.importer.validate import WAREHOUSE_ID
from app.models.base import utcnow
from app.models.job import JobOrder
from app.models.linkage import ImportSession, InventoryItem

__all__ = ["execute_import", "split_inventory_items", "split_job_orders"]

#: 文件类型 → 作业类型（16 A.4 #5：文件本身区分单据类型，源文件「类型」列不路由）。
_JOB_TYPE_BY_FILE_TYPE: Mapping[FileType, JobType] = {
    FileType.PO: JobType.INBOUND,
    FileType.DO: JobType.OUTBOUND,
}

#: 只分流为 JobOrder 的文件类型；INV 由 `split_inventory_items` 分流（快照 + cap 基线）。
_JOB_FILE_TYPES = (FileType.PO, FileType.DO)

#: 「库存记录时间」字符串的常见形态（CSV 导出）。
_DATETIME_TEXT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")

#: 文件类型 → 作业类型（16 A.4 #5：文件本身区分单据类型，源文件「类型」列不路由）。
_JOB_TYPE_BY_FILE_TYPE: Mapping[FileType, JobType] = {
    FileType.PO: JobType.INBOUND,
    FileType.DO: JobType.OUTBOUND,
}

#: 本任务只分流 PO/DO；INV 由任务 3.2 分流（快照 + cap 基线）。
_JOB_FILE_TYPES = (FileType.PO, FileType.DO)


def split_job_orders(
    rows: Sequence[Mapping[str, Any]],
    mapping: MappingResult,
    *,
    file_type: FileType,
    warehouse_id: str,
) -> list[JobOrder]:
    """PO/DO 行 → JobOrder（PENDING）。纯函数：不落库、不迁移状态。

    `job_type` 由 `FileType` 派生（16 A.4 #5）；`batch_no` 留空（PO/DO 模版没有批号列，
    入库单的批号由系统在入库单建立时按生产批规则生成 —— `JobOrder.batch_no` 注释）；
    `bulk_batch_no` 留空（导入入队时还没有批量 —— `JobOrder.bulk_batch_no` 注释）。
    """
    job_type = _JOB_TYPE_BY_FILE_TYPE[file_type]
    orders: list[JobOrder] = []
    for row in rows:
        orders.append(
            JobOrder(
                warehouse_id=warehouse_id,
                order_no=_as_text(_value(row, mapping, "order_no")),
                line_no=_as_text(_value(row, mapping, "line_no")),
                job_type=job_type,
                material_code=_as_text(_value(row, mapping, "material_code")),
                material_name=_as_optional_text(_value(row, mapping, "material_name")),
                qty=_to_int(_value(row, mapping, "qty")),
                batch_no=None,
            )
        )
    return orders


def split_inventory_items(
    rows: Sequence[Mapping[str, Any]],
    mapping: MappingResult,
    *,
    warehouse_id: str,
) -> list[InventoryItem]:
    """INV 行 → InventoryItem（`snapshot_id` 留空，由 `establish_baseline` 回填）。

    纯函数：不落库、不迁移状态。字段取自源行 —— 库位号按 6 位文本（`as_location_code`，
    前导 0 不丢）、数量按整数、库存记录时间按 datetime。`production_date` 恒 `None`：
    16 A.1 的 INV 模版已移除「生产日期」列（linkage.py `InventoryItem.production_date`
    注释：17 §3.3 列了它、A.1 已移除 → 可空）。

    `snapshot_time` 解析自文件「库存记录时间」列 —— 与 `ImportSession.data_time` 是同一
    时点（16 §七「数据时点须与标注时点一致」），两处列名不同只是来源不同（会话侧是用户
    标注、明细侧是文件字段），不是两个概念。解析失败抛 `ValueError`（口径异常，由
    execute 的 savepoint 转成整批回退，fail-safe 而非静默）。
    """
    items: list[InventoryItem] = []
    for row in rows:
        items.append(
            InventoryItem(
                warehouse_id=warehouse_id,
                location_code=as_location_code(_value(row, mapping, "location_code")),
                material_code=_as_text(_value(row, mapping, "material_code")),
                material_name=_as_optional_text(_value(row, mapping, "material_name")),
                batch_no=_as_text(_value(row, mapping, "batch_no")),
                production_date=None,
                item_status=_as_text(_value(row, mapping, "item_status")),
                qty=_to_int(_value(row, mapping, "qty")),
                snapshot_time=_as_datetime(_value(row, mapping, "snapshot_time")),
            )
        )
    return items


def execute_import(
    db: Session,
    session_row: ImportSession,
    files: Sequence[SourceFile],
    *,
    warehouse_id: str = WAREHOUSE_ID,
) -> ImportSession:
    """`VALIDATED → IMPORTING → IMPORTED → [BASELINE]`，分流三类文件。

    - 入口要求 `VALIDATED`（点「执行导入」）。非法入口抛 `StateConflict`（try 之外）。
    - PO/DO → JobOrder（`IMPORTING → IMPORTED`，savepoint 内）；INV → InventoryItem 并
      触发 `establish_baseline`（`IMPORTED → BASELINE`）。无 INV 则停在 `IMPORTED`。
    - 写入失败（唯一键冲突 / 业务校验）整体回滚该批，退回 `FAILED` —— 不产生半成品。

    返回**同一个** `session_row`（成功时 `BASELINE` 或 `IMPORTED`，失败时 `FAILED`），
    调用方按 `session_row.status` 区分。基线重算失败（`establish_baseline`）原样上抛，
    不回退状态（无 `IMPORTED → FAILED` 回边），由调用方整体回滚并提示重导。不 commit。
    """
    session_row.status = assert_import_transition(session_row.status, ImportStatus.IMPORTING)
    db.flush()

    try:
        orders = _split_job_orders_from_files(files, warehouse_id=warehouse_id)
        items = _split_inventory_from_files(files, warehouse_id=warehouse_id)
        with db.begin_nested():
            for order in orders:
                db.add(order)
            session_row.status = assert_import_transition(session_row.status, ImportStatus.IMPORTED)
            session_row.imported_at = utcnow()
            db.flush()
    except (DomainError, IntegrityError):
        # savepoint 已整体回滚（载入的 JobOrder、IMPORTED 迁移都没了），退回 FAILED。
        # 只收「执行失败」（唯一键冲突 / 解析探测等业务校验）；非预期异常原样上抛。
        db.expire(session_row)  # 内存态回数据库（savepoint 已回滚，DB 上是 IMPORTING）
        session_row.status = assert_import_transition(session_row.status, ImportStatus.FAILED)
        db.flush()
        return session_row

    # INV 在场 → 建 cap 基线（IMPORTED → BASELINE）。items 不在上面的 savepoint 里落库，
    # 它的 snapshot_id 由 establish_baseline 建快照后回填（见模块 docstring「事务与回滚」）。
    if items:
        establish_baseline(db, import_session=session_row, items=items)
    return session_row


def _split_job_orders_from_files(
    files: Sequence[SourceFile], *, warehouse_id: str
) -> list[JobOrder]:
    """把一批文件里 PO/DO 的行解析、映射并分流成 JobOrder（INV 跳过）。"""
    orders: list[JobOrder] = []
    for source in files:
        if source.file_type not in _JOB_FILE_TYPES:
            continue
        rows, mapping = parse_source(source)
        orders.extend(
            split_job_orders(rows, mapping, file_type=source.file_type, warehouse_id=warehouse_id)
        )
    return orders


def _split_inventory_from_files(
    files: Sequence[SourceFile], *, warehouse_id: str
) -> list[InventoryItem]:
    """把一批文件里 INV 的行解析、映射并分流成 InventoryItem（PO/DO 跳过）。"""
    items: list[InventoryItem] = []
    for source in files:
        if source.file_type is not FileType.INV:
            continue
        rows, mapping = parse_source(source)
        items.extend(split_inventory_items(rows, mapping, warehouse_id=warehouse_id))
    return items


def _as_datetime(value: Any) -> datetime:
    """把「库存记录时间」归一成 datetime。

    `datetime` / `date` 直取（date → 零点）；字符串按 `_DATETIME_TEXT_FORMATS` 逐格式试。
    解析不出抛 `ValueError` —— 口径异常，由 execute 的 savepoint 转成整批回退，而非静默
    存一个假时点。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        text = value.strip()
        for fmt in _DATETIME_TEXT_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"无法解析库存记录时间：{value!r}")
    raise ValueError(f"库存记录时间值类型不可归一：{type(value).__name__}")


def _value(row: Mapping[str, Any], mapping: MappingResult, field: str) -> Any:
    """按映射从源行取内部字段的值；该字段未被映射（选填缺失）时为 `None`。"""
    src = mapping.field_to_source.get(field)
    if src is None:
        return None
    return row.get(src)


def _as_text(value: Any) -> str:
    """csv 字符串 / xlsx 数值归一成文本。空值兜底成 `""`（上层已保证非空，这里只兜底）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_optional_text(value: Any) -> str | None:
    """选填文本列（品名）：缺失 / 空串 → `None`。"""
    text = _as_text(value)
    return text or None


def _to_int(value: Any) -> int:
    """把 csv 字符串 / xlsx 数值 / 整数浮点归一成 int；不可归一抛 `ValueError`。"""
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            f = float(text)  # "500.0" 这类也接受
            if f.is_integer():
                return int(f)
            raise ValueError from None
    raise ValueError
