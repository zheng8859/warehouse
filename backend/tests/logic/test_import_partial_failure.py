"""部分文件失败隔离（tasks.md 3.3 的验证）。

事实来源：16-数据衔接与 cap 自维护 §4.4（三类文件独立校验与导入）
          spec `data-import`「部分文件失败隔离」（Scenario：快照失败不阻断 PO/DO 载入）
          openspec/changes/data-import/design.md D4（某类失败不影响他类）

口径（与 spec 逐字对齐）：

- **三类文件独立校验与导入**：某类失败不得影响其他类。库存快照失败 = 无 cap 基线、
  容量受限，但 PO/DO 照常载入；回执明确「成功 N 类 / 失败 M 类」，不静默吞失败。
- **会话级（时点）失败仍阻断一切**：时点缺失 / 晚于当天时，即使某些文件校验通过，
  也不能执行导入 —— 那才是不带病入库的底线。
- **全部文件失败 = FAILED**：没有可导的文件，会话停在 FAILED 等修正重校验。

分两组钉住：

1. **纯决策**（`SessionValidation` 的 `session_passed` / `passed_files` / `failed_files` /
   `importable`、`serialize_receipt` 的「成功 N/失败 M」、`passed_filenames`）：喂一份
   两份过、一份挂的混合批次，断言逐文件结论与汇总计数。
2. **状态编排**（`run_validation`）：PO/DO 通过 + INV 失败 → 仍 `VALIDATED`（可执行）；
   全失败 → `FAILED`；时点非法 + 文件通过 → `FAILED`（会话级阻断优先）。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.enums import FileType, ImportStatus
from app.importer.session import (
    SourceFile,
    passed_filenames,
    run_validation,
    serialize_receipt,
    validate_files,
)
from app.models.linkage import ImportSession

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 0, 0)
TODAY = date(2026, 9, 14)

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

# INV 8 列（16 A.1）：仓库号/库位号/料号/品名/批号/状态/数量/库存记录时间。
_INV_HEADER = "仓库号,库位号,料号,品名,批号,状态,数量,库存记录时间"
GOOD_INV_CSV = f"{_INV_HEADER}\nGTJ10036,010104,M1,可乐,B1,合格,40,2026-09-08\n"
BAD_INV_CSV = f"{_INV_HEADER}\nGTJ10036,010104,M1,可乐,,合格,40,2026-09-08\n"  # 批号为空


def _csv(file_type: FileType, filename: str, text: str) -> SourceFile:
    return SourceFile(file_type=file_type, filename=filename, content=text.encode("utf-8"))


def _new_session(data_time: datetime = NOW) -> ImportSession:
    return ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=data_time,
    )


# ------------------------------------------------------------------ 纯决策：逐文件结论 + 汇总

def test_mixed_batch_isolates_failed_file() -> None:
    """PO/DO 通过 + INV 失败：`importable` 为真，通过/失败文件各自归位。"""
    validation = validate_files(
        [
            _csv(FileType.PO, "PO.csv", PO_CSV),
            _csv(FileType.DO, "DO.csv", DO_CSV),
            _csv(FileType.INV, "INV.csv", BAD_INV_CSV),
        ],
        data_time=NOW,
        now=TODAY,
    )
    assert validation.session_passed is True  # 时点有效
    assert validation.importable is True      # 至少一份通过 → 可执行
    assert [f.filename for f in validation.passed_files] == ["PO.csv", "DO.csv"]
    assert [f.filename for f in validation.failed_files] == ["INV.csv"]
    # 各文件状态仍独立可读。
    assert [f.status for f in validation.files] == ["PASSED", "PASSED", "FAILED"]


def test_receipt_reports_success_and_failure_counts() -> None:
    """回执明确「成功 2 类 / 失败 1 类」：`passed` 为可执行信号，附成功/失败类数。"""
    validation = validate_files(
        [
            _csv(FileType.PO, "PO.csv", PO_CSV),
            _csv(FileType.DO, "DO.csv", DO_CSV),
            _csv(FileType.INV, "INV.csv", BAD_INV_CSV),
        ],
        data_time=NOW,
        now=TODAY,
    )
    receipt = serialize_receipt("IMP-20260908-01", NOW, validation)

    assert receipt["passed"] is True            # 可执行（部分失败不阻断）
    assert receipt["success_files"] == 2
    assert receipt["failed_files"] == 1
    assert [f["status"] for f in receipt["files"]] == ["PASSED", "PASSED", "FAILED"]


def test_passed_filenames_extracts_passed_only() -> None:
    """`passed_filenames` 从回执里只取「PASSED」文件名，失败/缺状态不计入。"""
    receipt = {
        "files": [
            {"filename": "PO.csv", "status": "PASSED"},
            {"filename": "DO.csv", "status": "PASSED"},
            {"filename": "INV.csv", "status": "FAILED"},
        ],
    }
    assert passed_filenames(receipt) == {"PO.csv", "DO.csv"}


def test_passed_filenames_absent_receipt_is_empty() -> None:
    """无回执（直接以 VALIDATED 会话调用 execute）→ 空集，交由调用方按「不过滤」处置。"""
    assert passed_filenames(None) == frozenset()
    assert passed_filenames({"issues": []}) == frozenset()


# ------------------------------------------------------------------ 状态编排：部分失败仍可执行

def test_run_validation_partial_failure_still_validated(session: Session) -> None:
    """PO/DO 通过 + INV 失败 → 会话仍 `VALIDATED`（可执行导入），回执记「成功 2/失败 1」。"""
    row = _new_session()
    session.add(row)
    session.flush()

    run_validation(
        session,
        row,
        [
            _csv(FileType.PO, "PO.csv", PO_CSV),
            _csv(FileType.DO, "DO.csv", DO_CSV),
            _csv(FileType.INV, "INV.csv", BAD_INV_CSV),
        ],
        now=TODAY,
    )

    assert row.status is ImportStatus.VALIDATED
    assert row.receipt_json["passed"] is True
    assert row.receipt_json["success_files"] == 2
    assert row.receipt_json["failed_files"] == 1


def test_run_validation_all_failed_stays_failed(session: Session) -> None:
    """全部文件失败（无可导文件）→ `FAILED`，可修正后重校验。"""
    row = _new_session()
    session.add(row)
    session.flush()

    run_validation(session, row, [_csv(FileType.INV, "INV.csv", BAD_INV_CSV)], now=TODAY)

    assert row.status is ImportStatus.FAILED
    assert row.receipt_json["passed"] is False
    assert row.receipt_json["success_files"] == 0
    assert row.receipt_json["failed_files"] == 1


def test_run_validation_time_failure_blocks_even_with_passing_files(session: Session) -> None:
    """会话级时点非法 → 即使 PO 通过也 `FAILED`（不带病入库的底线高于部分隔离）。

    「成功 N/失败 M」是**文件级**回执汇总（时点正交）：PO 文件本身通过，故 `success_files`
    仍为 1；但会话级时点阻断使 `passed=False`、会话停在 `FAILED`。
    """
    row = _new_session(data_time=datetime(2026, 9, 15, 0, 0))  # 晚于当天
    session.add(row)
    session.flush()

    run_validation(session, row, [_csv(FileType.PO, "PO.csv", PO_CSV)], now=TODAY)

    assert row.status is ImportStatus.FAILED
    assert row.receipt_json["passed"] is False
    assert row.receipt_json["success_files"] == 1
    assert row.receipt_json["failed_files"] == 0
