"""执行导入分流：PO/DO → JobOrder（**编排层：做 IO + 一次原子写入**）。

事实来源：16-数据衔接与 cap 自维护 §3（VALIDATED→IMPORTING→IMPORTED/FAILED）· §4.4
          （执行导入分流）· A.4（PO/DO 分流只做两件事，PENDING 初值）
          spec `data-import`「建立基准（执行导入分流）」（写入失败回滚该批）
          openspec/changes/data-import/design.md D4（三类文件独立导入，写入失败回滚该批）
          CLAUDE.md §四（写并发由乐观锁版本号保证）

## 职责边界

`execute_import` 与 `session.run_validation` 对称：后者走 `DRAFT → VALIDATING →
VALIDATED|FAILED`，前者走 `VALIDATED → IMPORTING → IMPORTED|FAILED`。PO/DO 分流
**只做两件事**（16 A.4 #2）：

1. 载入队列 / 任务：PO → `JobOrder(job_type=INBOUND)`（入库待推荐队列）、
   DO → `JobOrder(job_type=OUTBOUND)`（出库拣配任务），业务状态初值 `PENDING`。
2. 生成行唯一键 `(warehouse_id, job_type, order_no, line_no)` —— 这正是 `JobOrder`
   表的联合唯一约束，重复导入撞它即被拦下。

**不触发** cap 重算 / ABC 派生 —— 那是 INV 分流（任务 3.2）与脚本通道（任务 5.1）的事。

## 事务与回滚

沿用 `confirm.py` 的 savepoint 手法：`VALIDATED → IMPORTING` 的守卫与 flush 在 try 之外
（初始状态不对是「调用方过期」，不是「执行失败」，不该被降级吞掉）；「载入 JobOrder →
`IMPORTING → IMPORTED`」包在 `begin_nested()`（savepoint）里，任一步失败整体回滚，再走
`IMPORTING → FAILED` 回边。savepoint 只滚 DB、不滚 ORM 对象，故回退后先 `expire` 让
`session_row` 回到 `IMPORTING` 再迁移。不 commit —— 事务边界属于 API 路由。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import FileType, ImportStatus, JobType
from app.core.errors import DomainError
from app.core.import_state import assert_import_transition
from app.importer.mapping import MappingResult
from app.importer.session import SourceFile, parse_source
from app.importer.validate import WAREHOUSE_ID
from app.models.base import utcnow
from app.models.job import JobOrder
from app.models.linkage import ImportSession

__all__ = ["execute_import", "split_job_orders"]

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


def execute_import(
    db: Session,
    session_row: ImportSession,
    files: Sequence[SourceFile],
    *,
    warehouse_id: str = WAREHOUSE_ID,
) -> ImportSession:
    """`VALIDATED → IMPORTING → IMPORTED | FAILED`，把 PO/DO 分流为 JobOrder。

    - 入口要求 `VALIDATED`（点「执行导入」）。非法入口抛 `StateConflict`（try 之外）。
    - 只处理 PO/DO；INV 由任务 3.2 分流（本任务只载队列/任务，不建基准）。
    - 写入失败（唯一键冲突 / 业务校验）整体回滚该批，退回 `FAILED` —— 不产生半成品。

    返回**同一个** `session_row`（成功时 `IMPORTED`，失败时 `FAILED`），调用方按
    `session_row.status` 区分。不 commit。
    """
    session_row.status = assert_import_transition(session_row.status, ImportStatus.IMPORTING)
    db.flush()

    try:
        orders = _split_job_orders_from_files(files, warehouse_id=warehouse_id)
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


def _split_job_orders_from_files(
    files: Sequence[SourceFile], *, warehouse_id: str
) -> list[JobOrder]:
    """把一批文件里 PO/DO 的行解析、映射并分流成 JobOrder（INV 跳过，属任务 3.2）。"""
    orders: list[JobOrder] = []
    for source in files:
        if source.file_type not in _JOB_FILE_TYPES:
            continue
        rows, mapping = parse_source(source)
        orders.extend(
            split_job_orders(rows, mapping, file_type=source.file_type, warehouse_id=warehouse_id)
        )
    return orders


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
