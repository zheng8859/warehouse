"""会话编排 + 回执落库（tasks.md 2.5 的验证）。

事实来源：16-数据衔接与 cap 自维护 §3（状态机，FAILED→DRAFT 唯一回边）· §4.5 / 17 §10.4（回执）
          spec `data-import`「四层校验与阻断」（回执可展开到文件+列+行+原因）
          spec `data-import`「数据导入页数据层」（Scenario：修正文件后 FAILED→DRAFT 重新校验）
          openspec/changes/data-import/design.md D2（session.py 只做编排不掺业务规则）

分两组钉住：

1. **纯编排**（`validate_files` / `serialize_receipt`）：不连库，喂一份 INV CSV 就断言
   回执可展开到 `filename / column / row / reason` 级。
2. **状态编排**（`run_validation` / `return_to_draft`）：连内存库，钉住
   `DRAFT → VALIDATING → VALIDATED|FAILED` 与 `FAILED → DRAFT →（重校验）` 整条回边。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.enums import FileType, ImportStatus
from app.core.errors import StateConflict
from app.importer.session import (
    SESSION_SCOPE,
    SourceFile,
    return_to_draft,
    run_validation,
    serialize_receipt,
    validate_files,
)
from app.models.linkage import ImportSession

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 0, 0)
TODAY = date(2026, 9, 14)

_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"
_GOOD_ROW = "GTJ10036,010104,M1,可乐,B1,合格,40,2026-09-08"
_BAD_ROW = "GTJ10036,010104,M1,可乐,,合格,40,2026-09-08"  # 批号为空

INV_CSV = f"{_HEADER}\n{_GOOD_ROW}\n"
BAD_INV_CSV = f"{_HEADER}\n{_BAD_ROW}\n"


def _csv(file_type: FileType, filename: str, text: str) -> SourceFile:
    return SourceFile(file_type=file_type, filename=filename, content=text.encode("utf-8"))


def _new_session() -> ImportSession:
    return ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=NOW,
    )


# ------------------------------------------------------------------ 纯编排：回执明细

def test_validate_files_good_inv_passes() -> None:
    validation = validate_files(
        [_csv(FileType.INV, "INV.csv", INV_CSV)], data_time=NOW, now=TODAY
    )
    assert validation.passed is True
    summary = validation.files[0]
    assert (summary.rows, summary.fields_hit, summary.anomalies, summary.status) == (
        1, "7/7", 0, "PASSED",
    )


def test_validate_files_bad_inv_blocks_with_detail() -> None:
    validation = validate_files(
        [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], data_time=NOW, now=TODAY
    )
    assert validation.passed is False
    summary = validation.files[0]
    assert summary.status == "FAILED"
    assert summary.anomalies == 1

    issue = validation.report.blocking[0]
    assert (issue.filename, issue.column, issue.row, issue.reason) == (
        "INV.csv", "batch_no", 1, "批号不能为空",
    )


def test_time_issue_is_session_scoped_not_file_scoped() -> None:
    """会话级时点检查跑一次，其回执条目用 `SESSION_SCOPE` 标会话，不套到某份文件。"""
    future = datetime(2026, 9, 15, 0, 0)
    validation = validate_files(
        [_csv(FileType.INV, "INV.csv", INV_CSV)], data_time=future, now=TODAY
    )
    assert validation.passed is False
    time_issue = next(i for i in validation.report.blocking if i.reason == "数据时点晚于当天")
    assert time_issue.filename == SESSION_SCOPE
    assert time_issue.row is None and time_issue.column is None


def test_serialize_receipt_is_expandable_to_file_column_row_reason() -> None:
    validation = validate_files(
        [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], data_time=NOW, now=TODAY
    )
    receipt = serialize_receipt("IMP-20260908-01", NOW, validation)

    assert receipt["session_id"] == "IMP-20260908-01"
    assert receipt["data_time"] == "2026-09-08"
    assert receipt["passed"] is False

    file_entry = receipt["files"][0]
    assert (file_entry["file_type"], file_entry["rows"], file_entry["fields_hit"]) == (
        "INV", 1, "7/7",
    )
    assert file_entry["status"] == "FAILED"

    # spec「回执可展开到文件 + 列 + 行 + 原因」：每条 issue 都带这四样。
    issue = receipt["issues"][0]
    assert issue["filename"] == "INV.csv"
    assert issue["column"] == "batch_no"
    assert issue["row"] == 1
    assert issue["reason"] == "批号不能为空"
    assert issue["layer"] == "业务"


def test_serialize_receipt_omits_none_column_and_row() -> None:
    """会话级时点条目没有列/行概念，序列化后 `column` / `row` 为 None（可展开但无值）。"""
    future = datetime(2026, 9, 15, 0, 0)
    validation = validate_files([], data_time=future, now=TODAY)
    receipt = serialize_receipt("IMP-20260908-01", future, validation)
    issue = next(i for i in receipt["issues"] if i["reason"] == "数据时点晚于当天")
    assert issue["column"] is None and issue["row"] is None


# ------------------------------------------------------------------ 状态编排：回边与落库

def test_run_validation_draft_to_validated(session: Session) -> None:
    row = _new_session()
    session.add(row)
    session.flush()

    run_validation(session, row, [_csv(FileType.INV, "INV.csv", INV_CSV)], now=TODAY)

    assert row.status is ImportStatus.VALIDATED
    assert row.validating_at is not None
    assert row.validated_at is not None
    assert row.receipt_json["passed"] is True
    assert row.receipt_json["files"][0]["status"] == "PASSED"


def test_run_validation_draft_to_failed(session: Session) -> None:
    row = _new_session()
    session.add(row)
    session.flush()

    run_validation(session, row, [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], now=TODAY)

    assert row.status is ImportStatus.FAILED
    assert row.receipt_json["passed"] is False
    assert row.receipt_json["issues"][0]["reason"] == "批号不能为空"


def test_return_to_draft_from_failed(session: Session) -> None:
    row = _new_session()
    session.add(row)
    session.flush()
    run_validation(session, row, [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.FAILED

    return_to_draft(session, row)
    assert row.status is ImportStatus.DRAFT


def test_full_cycle_failed_to_draft_then_revalidated(session: Session) -> None:
    """spec Scenario「修正文件后重新校验」：FAILED → DRAFT → 重校验 → VALIDATED。"""
    row = _new_session()
    session.add(row)
    session.flush()

    run_validation(session, row, [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.FAILED

    return_to_draft(session, row)
    assert row.status is ImportStatus.DRAFT

    run_validation(session, row, [_csv(FileType.INV, "INV.csv", INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.VALIDATED
    assert row.receipt_json["passed"] is True


def test_failed_cannot_revalidate_without_returning_to_draft(session: Session) -> None:
    """`FAILED → VALIDATING` 没有边 —— 必须先 `FAILED → DRAFT` 再重校验。"""
    row = _new_session()
    session.add(row)
    session.flush()
    run_validation(session, row, [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.FAILED

    with pytest.raises(StateConflict):
        run_validation(session, row, [_csv(FileType.INV, "INV.csv", INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.FAILED  # 被拒后状态不动


def test_run_validation_requires_draft_entry(session: Session) -> None:
    """`VALIDATED → VALIDATING` 没有边：校验过的会话不能再次「开始校验」。"""
    row = _new_session()
    session.add(row)
    session.flush()
    run_validation(session, row, [_csv(FileType.INV, "INV.csv", INV_CSV)], now=TODAY)
    assert row.status is ImportStatus.VALIDATED

    with pytest.raises(StateConflict):
        run_validation(session, row, [_csv(FileType.INV, "INV.csv", INV_CSV)], now=TODAY)
