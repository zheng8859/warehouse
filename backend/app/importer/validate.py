"""四层校验（**纯函数，无 IO**）。

事实来源：16-数据衔接与 cap 自维护 §二（四层校验）与 §4.4（校验失败阻断）
          spec `data-import`「四层校验与阻断」的两个 Scenario
          openspec/changes/data-import/design.md D3

四层：**结构**（可解析、非空）→ **字段**（模板必填列 100% 命中）→ **时点**（已标注且
不晚于当天）→ **业务**（数量 > 0 / 库位号 6 位 / 批号非空 / 仓库号一致 / 状态非空）。

**任一阻断级异常即阻断**（不得带病入库），回执可展开到「文件 + 列 + 行 + 原因」。
**选填缺失降级不阻断**（回执标注，降级不静默）。

## 一处与 spec 措辞的口径对齐（评审要看）

spec 写「状态取值在枚举内」，但 `ItemStatus`（`app/core/enums.py`）的权威口径是
**源数据驱动、不穷举**（17 §九⑪：「取值以 GTJ10036 导出为准，校验时不得把它们当作
穷举集合」；`master_data.py` 亦有同款「不要把 item_status 的已知取值抄成 CHECK」）。
故本层对状态只做**非空**校验 —— 空状态阻断，非空但不在「合格/待检/冻结」内的取值
（如「在途」）**放行**。穷举校验会把源数据里真实存在、但不在示例枚举内的状态拦下，
反致「带病入库」的另一面：**该入的进不来**。

## 职责边界

本层只产出 `ValidationReport`，**不抛异常、不落库、不改状态**。抛 `ValidationBlocked`
阻断、把回执写进 `ImportSession.receipt_json`、把状态推进到 `FAILED` 是编排层
（`session.py`）的事 —— 这样「校验」与「处置」分离，校验本身可被纯函数单测覆盖。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from app.core.enums import FileType
from app.importer.loaders import as_datetime, as_location_code
from app.importer.mapping import MappingResult

__all__ = [
    "IssueLevel",
    "ValidationIssue",
    "ValidationLayer",
    "ValidationReport",
    "validate_business",
    "validate_fields",
    "validate_rows",
    "validate_structure",
    "validate_time",
]

#: 单厂仓库号（16 A.1「仓库号」直映射 `warehouse_id`）。业务层「仓库号与单厂一致」据此判。
WAREHOUSE_ID = "GTJ10036"


class IssueLevel(str, Enum):
    """回执条目的级别。阻断级致整批失败；降级级只标注、不阻断。"""

    BLOCKING = "blocking"
    DEGRADED = "degraded"


class ValidationLayer(str, Enum):
    """四层的名字，落进回执的 `layer` 字段，供看板按层聚合。"""

    STRUCTURE = "结构"
    FIELD = "字段"
    TIME = "时点"
    BUSINESS = "业务"


@dataclass(frozen=True)
class ValidationIssue:
    """一条回执明细，可展开到「文件 + 列 + 行 + 原因」级。

    - `row` 是**数据行序号**（从 1 起，不含表头），无行概念（结构/字段/时点层）为 `None`。
    - `column` 是内部字段名（如 `location_code`），无列概念为 `None`。
    """

    level: IssueLevel
    layer: ValidationLayer
    filename: str
    column: str | None
    row: int | None
    reason: str


@dataclass(frozen=True)
class ValidationReport:
    """一次校验的完整回执。`passed` 只看阻断级，降级级不影响通过。"""

    issues: tuple[ValidationIssue, ...]

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.level is IssueLevel.BLOCKING)

    @property
    def degraded(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.level is IssueLevel.DEGRADED)

    @property
    def passed(self) -> bool:
        return not self.blocking


def _issue(
    level: IssueLevel, layer: ValidationLayer, filename: str, reason: str,
    *, column: str | None = None, row: int | None = None,
) -> ValidationIssue:
    return ValidationIssue(level=level, layer=layer, filename=filename, column=column, row=row, reason=reason)


def validate_rows(
    *,
    rows: Sequence[Mapping[str, Any]],
    mapping: MappingResult,
    file_type: FileType,
    data_time: datetime | None,
    warehouse_id: str = WAREHOUSE_ID,
    filename: str,
    now: date | None = None,
) -> ValidationReport:
    """跑四层校验，返回完整回执。纯计算，不抛异常、不落库。"""
    issues = (
        validate_structure(rows, filename=filename)
        + validate_fields(mapping, file_type=file_type, filename=filename)
        + validate_time(data_time, filename=filename, now=now)
        + validate_business(rows, mapping, file_type=file_type, warehouse_id=warehouse_id, filename=filename)
    )
    return ValidationReport(issues=tuple(issues))


# ------------------------------------------------------------------ 结构层

def validate_structure(rows: Sequence[Mapping[str, Any]], *, filename: str) -> list[ValidationIssue]:
    """结构层：可解析、非空。「可解析」由 detect / loaders 抛异常承担，这里只管「非空」。"""
    if not rows:
        return [
            _issue(IssueLevel.BLOCKING, ValidationLayer.STRUCTURE, filename, "文件无数据行（仅表头或为空）")
        ]
    return []


# ------------------------------------------------------------------ 字段层

def validate_fields(mapping: MappingResult, *, file_type: FileType, filename: str) -> list[ValidationIssue]:
    """字段层：必填列 100% 命中。缺失逐列列出；品名选填缺失降级标注。"""
    issues: list[ValidationIssue] = []
    for field in sorted(mapping.missing_required):
        issues.append(
            _issue(IssueLevel.BLOCKING, ValidationLayer.FIELD, filename, f"缺失必填列：{field}", column=field)
        )
    if "material_name" not in mapping.field_to_source:
        issues.append(
            _issue(IssueLevel.DEGRADED, ValidationLayer.FIELD, filename, "选填列「品名」缺失，相关展示降级", column="material_name")
        )
    return issues


# ------------------------------------------------------------------ 时点层

def validate_time(data_time: datetime | None, *, filename: str, now: date | None = None) -> list[ValidationIssue]:
    """时点层：已标注且不晚于当天。缺失与未来时点都阻断（spec「时点缺失或晚于当天即阻断」）。"""
    today = now if now is not None else date.today()
    if data_time is None:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.TIME, filename, "数据时点为必填，未标注")]
    if data_time.date() > today:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.TIME, filename, "数据时点晚于当天")]
    return []


# ------------------------------------------------------------------ 业务层

def validate_business(
    rows: Sequence[Mapping[str, Any]],
    mapping: MappingResult,
    *,
    file_type: FileType,
    warehouse_id: str,
    filename: str,
) -> list[ValidationIssue]:
    """业务层：逐行、逐列的口径校验。返回全部阻断级问题（降级不在此层产生）。"""
    issues: list[ValidationIssue] = []
    for row_no, row in enumerate(rows, start=1):
        issues.extend(_check_quantity(_value(row, mapping, "qty"), row_no, filename))
        issues.extend(_check_warehouse(_value(row, mapping, "warehouse_no"), row_no, filename, warehouse_id))

        if file_type is FileType.INV:
            issues.extend(_check_location(_value(row, mapping, "location_code"), row_no, filename))
            issues.extend(_check_batch(_value(row, mapping, "batch_no"), row_no, filename))
            issues.extend(_check_status(_value(row, mapping, "item_status"), row_no, filename))
            issues.extend(_check_snapshot_time(_value(row, mapping, "snapshot_time"), row_no, filename))
        else:
            issues.extend(_check_required_text(_value(row, mapping, "order_no"), "单据号码", "order_no", row_no, filename))
            issues.extend(_check_required_text(_value(row, mapping, "line_no"), "行号", "line_no", row_no, filename))
    return issues


def _value(row: Mapping[str, Any], mapping: MappingResult, field: str) -> Any:
    src = mapping.field_to_source.get(field)
    if src is None:
        return None
    return row.get(src)


def _check_quantity(raw: Any, row_no: int, filename: str) -> list[ValidationIssue]:
    """数量 > 0。空、非整数、≤0 都阻断。"""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "数量为空", column="qty", row=row_no)]
    try:
        qty = _to_int(raw)
    except ValueError:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "数量非整数", column="qty", row=row_no)]
    if qty <= 0:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "数量必须大于 0", column="qty", row=row_no)]
    return []


def _check_warehouse(raw: Any, row_no: int, filename: str, warehouse_id: str) -> list[ValidationIssue]:
    """仓库号与单厂一致（GTJ10036）。"""
    text = str(raw).strip() if raw is not None else ""
    if text != warehouse_id:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, f"仓库号须为 {warehouse_id}", column="warehouse_no", row=row_no)]
    return []


def _check_location(raw: Any, row_no: int, filename: str) -> list[ValidationIssue]:
    """库位号按 6 位文本可切片出巷道（前 2 位）。归一失败或非 6 位都阻断。"""
    try:
        text = as_location_code(raw)
    except ValueError:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "库位号值不可归一为文本", column="location_code", row=row_no)]
    if len(text) != 6:
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "库位号必须为 6 位文本", column="location_code", row=row_no)]
    return []


def _check_batch(raw: Any, row_no: int, filename: str) -> list[ValidationIssue]:
    """批号非空（16 A.1：批号是库存行标识之一）。"""
    text = str(raw).strip() if raw is not None else ""
    if text == "":
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "批号不能为空", column="batch_no", row=row_no)]
    return []


def _check_status(raw: Any, row_no: int, filename: str) -> list[ValidationIssue]:
    """状态非空（源数据驱动、不穷举 —— 见模块 docstring）。"""
    text = str(raw).strip() if raw is not None else ""
    if text == "":
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "状态不能为空", column="item_status", row=row_no)]
    return []


def _check_snapshot_time(raw: Any, row_no: int, filename: str) -> list[ValidationIssue]:
    """库存记录时间必填、且能按支持格式解析（INV 行级时点）。

    与执行分流 `split_inventory_items` 用**同一个** `as_datetime` 原语 —— 校验与执行
    口径必须一致：这里放行了，执行时就必须能解析。曾因执行侧私有格式表少了无分隔符
    `%Y%m%d`（WMS 导出的 `20260915`），而校验层又完全不查本列，导致校验 PASSED 的
    文件在「执行导入」时抛 ValueError 崩成 HTTP 500。
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "库存记录时间为空", column="snapshot_time", row=row_no)]
    try:
        as_datetime(raw)
    except (ValueError, TypeError):
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, "库存记录时间格式无法解析（支持 2026-09-15 / 2026/09/15 / 20260915 等）", column="snapshot_time", row=row_no)]
    return []


def _check_required_text(raw: Any, label: str, column: str, row_no: int, filename: str) -> list[ValidationIssue]:
    """单号 / 行号非空（模型 NOT NULL 且入唯一键）。"""
    text = str(raw).strip() if raw is not None else ""
    if text == "":
        return [_issue(IssueLevel.BLOCKING, ValidationLayer.BUSINESS, filename, f"{label}不能为空", column=column, row=row_no)]
    return []


def _to_int(value: Any) -> int:
    """把 csv 字符串 / xlsx 数值 / 整数浮点 归一成 int；不可归一抛 ValueError。"""
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
            f = float(text)  # "40.0" 这类也接受
            if f.is_integer():
                return int(f)
            raise ValueError from None
    raise ValueError
