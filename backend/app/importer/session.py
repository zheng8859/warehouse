"""会话编排 + 回执落库（**编排层：做 IO，不掺业务规则**）。

事实来源：16-数据衔接与 cap 自维护 §3（状态机）· §4（8 步管线）· §4.5 / 17 §10.4（回执）
          spec `data-import`「四层校验与阻断」「数据导入页数据层」（Scenario：失败可修正重校验）
          openspec/changes/data-import/design.md D2（session.py 只做编排不掺业务规则）

## 职责边界（为什么它薄）

`detect / loaders / mapping / validate` 四个模块是**纯函数**（不碰磁盘、不碰库），各自被
`tests/logic` 锁定。`session.py` 是唯一做 IO 的一层，但它**不掺业务规则**：

- `validate_files`：把一批 `SourceFile` 按流水线 `detect → loaders → mapping → validate`
  串起来，聚合成一份 `SessionValidation`（每个文件一行摘要 + 一份可展开到行级的完整
  回执）。**纯函数**：不迁移状态、不落库 —— 它返回结果，让调用方决定怎么处置。
- `serialize_receipt`：把 `SessionValidation` 序列化成 `receipt_json`（17 §10.4 形状，
  外加 `issues` 明细）。**纯函数**。
- `run_validation` / `return_to_draft`：状态迁移 + 里程碑时间戳 + 把回执写进
  `ImportSession.receipt_json`。这两个做 IO（`db.flush()`），**不 commit** —— 事务边界
  属于调用方（API 路由），这样「cap 与台账同事务」这类跨实体写入才可能成立。

于是「校验」与「处置」分离：校验本身可被纯函数单测覆盖，状态推进与落库只在这一层，
且只在两个函数里。

## 时点检查的归属

`data_time` 是**会话级**（时点卡一次标注，`ImportSession.data_time` 非空），不是某份文件
的字段。故 `validate_time` 在 `validate_files` 里**只跑一次**（对整批），其回执条目的
`filename` 用哨兵 `SESSION_SCOPE` 标「会话级」，而非套到某份文件头上 —— 套错文件会让
运维在错误的文件里找一个不存在的时点列。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import FileType, ImportStatus
from app.core.errors import ValidationBlocked
from app.core.import_state import assert_import_transition
from app.importer.detect import detect
from app.importer.loaders import parse_rows
from app.importer.mapping import REQUIRED_BY_TYPE, MappingResult, build_mapping
from app.importer.validate import (
    WAREHOUSE_ID,
    IssueLevel,
    ValidationIssue,
    ValidationLayer,
    ValidationReport,
    validate_business,
    validate_fields,
    validate_structure,
    validate_time,
)
from app.models.base import utcnow
from app.models.linkage import ImportSession

__all__ = [
    "SESSION_SCOPE",
    "FileSummary",
    "SessionValidation",
    "SourceFile",
    "parse_source",
    "passed_filenames",
    "return_to_draft",
    "run_validation",
    "serialize_receipt",
    "validate_files",
]

#: 会话级回执条目的 `filename` 哨兵（时点标注 / 未来的时点检查没有对应文件）。
SESSION_SCOPE = "(会话)"


@dataclass(frozen=True)
class SourceFile:
    """一份待校验的源文件：业务类别 + 文件名 + 内容字节。"""

    file_type: FileType
    filename: str
    content: bytes


@dataclass(frozen=True)
class FileSummary:
    """一份文件的校验摘要（对应 17 §10.4 回执表的一行，也对应 p2 回执表的一行）。"""

    file_type: FileType
    filename: str
    rows: int            # 成功解析的数据行数
    fields_hit: str      # "命中 / 模板要求列数"，如 "7/7"
    anomalies: int       # 该文件阻断级异常条数（口径异常可展开）
    passed: bool

    @property
    def status(self) -> str:
        """回执表「状态」列：通过 / 失败。"""
        return "PASSED" if self.passed else "FAILED"


@dataclass(frozen=True)
class SessionValidation:
    """一批文件的校验结果：每文件一行摘要 + 一份完整回执（含会话级时点条目）。

    `passed` 是「整批无任何阻断级异常」的旧口径（全绿）；`importable` 是「可执行导入」
    的新口径（部分文件失败隔离：会话级通过且至少一份文件通过）。两者并存 —— 前者仍是
    逐文件回执明细之外的总开关，后者决定「点执行导入」这一下能不能走（spec「部分文件
    失败隔离」：某类失败不影响他类，但时点非法仍阻断一切）。
    """

    files: tuple[FileSummary, ...]
    report: ValidationReport

    @property
    def passed(self) -> bool:
        return self.report.passed

    @property
    def session_passed(self) -> bool:
        """会话级通过：时点检查无阻断（没有任何 `SESSION_SCOPE` 的阻断级条目）。"""
        return all(i.filename != SESSION_SCOPE for i in self.report.blocking)

    @property
    def passed_files(self) -> tuple[FileSummary, ...]:
        """校验通过的文件（按传入顺序）。"""
        return tuple(f for f in self.files if f.passed)

    @property
    def failed_files(self) -> tuple[FileSummary, ...]:
        """校验失败的文件（按传入顺序）。"""
        return tuple(f for f in self.files if not f.passed)

    @property
    def importable(self) -> bool:
        """可执行导入：会话级通过 且 至少一份文件通过（部分文件失败隔离）。"""
        return self.session_passed and bool(self.passed_files)


def validate_files(
    files: Sequence[SourceFile],
    *,
    data_time: datetime,
    warehouse_id: str = WAREHOUSE_ID,
    now: date | None = None,
) -> SessionValidation:
    """对一批文件跑四层校验，聚合成 `SessionValidation`。纯函数，不迁移状态、不落库。

    - 时点检查跑一次（会话级，见模块 docstring）。
    - 每份文件走 `detect → loaders → mapping → validate`；探测失败（格式/编码不可识别）
      与字段/结构/业务阻断都落成该文件的回执条目，而非抛出 —— 这样一份回执能同时
      装下「三个文件各自过没过」，与「部分文件失败隔离」的编排天然对齐。
    """
    issues: list[ValidationIssue] = list(validate_time(data_time, filename=SESSION_SCOPE, now=now))

    summaries: list[FileSummary] = []
    for source in files:
        summary, file_issues = _validate_one(source, warehouse_id=warehouse_id)
        summaries.append(summary)
        issues.extend(file_issues)

    return SessionValidation(
        files=tuple(summaries),
        report=ValidationReport(issues=tuple(issues)),
    )


def parse_source(source: SourceFile) -> tuple[list[dict[str, Any]], MappingResult]:
    """一份文件的流水线前半段：探测 → 解析 → 映射。纯函数。

    探测失败（格式 / 编码不可识别）抛 `ValidationBlocked` —— 处置（阻断成回执还是上抛）
    由调用方决定：`_validate_one` 把它落成该文件的回执条目，`execute_import` 把它当成
    「执行失败」回退。校验与执行都要重跑这段，抽出来避免两处各自漂移。
    """
    detected = detect(source.filename, source.content)
    rows = parse_rows(source.content, detected)
    headers = list(rows[0].keys()) if rows else []
    mapping = build_mapping(headers, source.file_type)
    return rows, mapping


def _validate_one(source: SourceFile, *, warehouse_id: str) -> tuple[FileSummary, list[ValidationIssue]]:
    """一份文件的流水线：探测 → 解析 → 映射 → 校验。探测失败也算该文件失败。"""
    try:
        rows, mapping = parse_source(source)
    except ValidationBlocked as exc:
        issue = ValidationIssue(
            level=IssueLevel.BLOCKING,
            layer=ValidationLayer.STRUCTURE,
            filename=source.filename,
            column=None,
            row=None,
            reason=exc.message,
        )
        summary = FileSummary(
            file_type=source.file_type,
            filename=source.filename,
            rows=0,
            fields_hit=f"0/{len(REQUIRED_BY_TYPE[source.file_type])}",
            anomalies=1,
            passed=False,
        )
        return summary, [issue]

    file_issues = (
        validate_structure(rows, filename=source.filename)
        + validate_fields(mapping, file_type=source.file_type, filename=source.filename)
        + validate_business(
            rows, mapping, file_type=source.file_type, warehouse_id=warehouse_id, filename=source.filename
        )
    )

    blocking = sum(1 for i in file_issues if i.level is IssueLevel.BLOCKING)
    summary = FileSummary(
        file_type=source.file_type,
        filename=source.filename,
        rows=len(rows),
        fields_hit=_fields_hit_str(source.file_type, mapping),
        anomalies=blocking,
        passed=blocking == 0,
    )
    return summary, file_issues


def _fields_hit_str(file_type: FileType, mapping: MappingResult) -> str:
    """「命中 n / 模板要求列数 m」—— 分母是模版要求列数，不是系统内部字段总数（16 §4.5 注）。"""
    total = len(REQUIRED_BY_TYPE[file_type])
    hit = total - len(mapping.missing_required)
    return f"{hit}/{total}"


def serialize_receipt(session_no: str, data_time: datetime, validation: SessionValidation) -> dict:
    """把校验结果序列化成 `receipt_json`（17 §10.4 形状 + `issues` 可展开明细）。

    纯函数：只读 `SessionValidation`，不做 IO。`issues` 每条都带 `filename / column / row /
    reason`，正是 spec「回执可展开到文件 + 列 + 行 + 原因」要的那份明细。`passed` 是
    **可执行**信号（`importable`，部分失败不阻断），另附 `success_files` / `failed_files`
    两个计数，即 spec「部分文件失败隔离」要的「成功 N 类 / 失败 M 类」。
    """
    return {
        "session_id": session_no,
        "data_time": data_time.date().isoformat(),
        "files": [
            {
                "file_type": s.file_type.value,
                "filename": s.filename,
                "rows": s.rows,
                "fields_hit": s.fields_hit,
                "anomalies": s.anomalies,
                "status": s.status,
            }
            for s in validation.files
        ],
        "issues": [
            {
                "level": i.level.value,
                "layer": i.layer.value,
                "filename": i.filename,
                "column": i.column,
                "row": i.row,
                "reason": i.reason,
            }
            for i in validation.report.issues
        ],
        "passed": validation.importable,
        "success_files": len(validation.passed_files),
        "failed_files": len(validation.failed_files),
    }


def passed_filenames(receipt: Mapping[str, Any] | None) -> frozenset[str]:
    """从回执里提取「校验通过」的文件名集合（部分文件失败隔离的执行依据）。

    回执缺 `files` 明细或整体为 `None`（直接以 `VALIDATED` 会话调用 execute、未经
    `run_validation` 落回执）→ 返回空集。空集在 `execute._importable_files` 里的语义是
    「没有逐文件结论，全部视为可导」，不是「一个都不导」—— 后者只在回执列了逐文件失败
    时才成立（此时返回的非空集合自然只含通过的文件）。纯函数：只读回执、不做 IO。
    """
    if not receipt:
        return frozenset()
    return frozenset(
        f["filename"] for f in receipt.get("files", ()) if f.get("status") == "PASSED"
    )


def run_validation(
    db: Session,
    session_row: ImportSession,
    files: Sequence[SourceFile],
    *,
    warehouse_id: str = WAREHOUSE_ID,
    now: date | None = None,
) -> ImportSession:
    """一次校验的编排：`DRAFT → VALIDATING → VALIDATED | FAILED` + 回执落库。

    - 入口要求 `DRAFT`（点「开始校验」）。`FAILED` 想重校验必须先 `return_to_draft` ——
      那是唯一的回边，本函数不提供 `FAILED → VALIDATING` 的捷径（16 §3.1）。
    - 校验结论看 `validation.importable`（部分文件失败隔离）：会话级时点通过且至少一份
      文件通过 → `VALIDATED`（失败的某类文件不阻断他类，执行时被隔离）；否则 `FAILED`。
      降级级只进回执、不翻转状态。
    - `db.flush()` 让回执与状态**落库**（本事务内可读），**不 commit**（见模块 docstring）。
    """
    session_row.status = assert_import_transition(session_row.status, ImportStatus.VALIDATING)
    session_row.validating_at = utcnow()

    validation = validate_files(files, data_time=session_row.data_time, warehouse_id=warehouse_id, now=now)
    session_row.receipt_json = serialize_receipt(session_row.session_no, session_row.data_time, validation)

    target = ImportStatus.VALIDATED if validation.importable else ImportStatus.FAILED
    session_row.status = assert_import_transition(session_row.status, target)
    session_row.validated_at = utcnow()

    db.flush()
    return session_row


def return_to_draft(db: Session, session_row: ImportSession) -> ImportSession:
    """`FAILED → DRAFT`（16 §3.1 唯一的回边）：修正文件后重新校验。

    `db.flush()` 落库、不 commit。回执留在 `receipt_json` 里供前端展示上一次失败明细，
    重校验成功后由 `run_validation` 覆盖 —— 不回退 `receipt_json`，因为「上次为什么失败」
    仍是可追溯信息，且重校验本身就是一次新的 `VALIDATING → …` 迁移。
    """
    session_row.status = assert_import_transition(session_row.status, ImportStatus.DRAFT)
    db.flush()
    return session_row
