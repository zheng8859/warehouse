"""四层校验的契约测试（tasks.md 2.4 的验证）。

事实来源：16 §二（四层校验）、§4.4（校验失败阻断）
          spec `data-import`「四层校验与阻断」的两个 Scenario
          openspec/changes/data-import/design.md D3

校验是**纯函数**：不落库、不改状态，只产出 `ValidationReport`。本文件用构造的行字典
（表头 → 值）+ `mapping.build_mapping` 的结果喂给各层，逐条钉住「哪些阻断、哪些降级」。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.enums import FileType
from app.importer.mapping import build_mapping
from app.importer.validate import (
    IssueLevel,
    ValidationLayer,
    validate_business,
    validate_fields,
    validate_rows,
    validate_structure,
    validate_time,
)

pytestmark = pytest.mark.logic

INV_HEADERS = ["仓库号", "库位号", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
TODAY = date(2026, 9, 14)
DATA_TIME = datetime(2026, 9, 8, 0, 0)


def _mapping(headers: list[str] | None = None):
    return build_mapping(headers or INV_HEADERS, FileType.INV)


def _row(**overrides) -> dict:
    base = {
        "仓库号": "GTJ10036",
        "库位号": "010104",
        "料号": "M1",
        "品名": "可乐",
        "批号": "B1",
        "状态": "合格",
        "数量": 40,
        "库存记录时间": "2026-09-08",
    }
    base.update(overrides)
    return base


def _business_issues(rows, headers=None):
    return validate_business(rows, _mapping(headers), file_type=FileType.INV, warehouse_id="GTJ10036", filename="INV.csv")


# ------------------------------------------------------------------ 业务层：逐项阻断

def test_valid_inv_row_passes() -> None:
    report = validate_rows(
        rows=[_row()], mapping=_mapping(), file_type=FileType.INV,
        data_time=DATA_TIME, filename="INV.csv", now=TODAY,
    )
    assert report.passed is True
    assert report.blocking == ()


@pytest.mark.parametrize("qty", [0, -5])
def test_qty_zero_or_negative_blocks(qty: int) -> None:
    issues = _business_issues([_row(数量=qty)])
    assert any("数量必须大于 0" == i.reason for i in issues)


def test_qty_empty_blocks() -> None:
    issues = _business_issues([_row(数量="")])
    assert any("数量为空" == i.reason for i in issues)


def test_qty_non_integer_blocks() -> None:
    issues = _business_issues([_row(数量=40.5)])
    assert any("数量非整数" == i.reason for i in issues)


def test_batch_empty_blocks() -> None:
    issues = _business_issues([_row(批号="")])
    assert any("批号不能为空" == i.reason for i in issues)


def test_location_not_six_chars_blocks() -> None:
    """库位号非 6 位阻断；数字 10104 补前导 0 后为 6 位则放行。"""
    issues = _business_issues([_row(库位号="12345")])
    assert any("库位号必须为 6 位文本" == i.reason for i in issues)

    # 数字 10104 → as_location_code → "010104"（6 位），不阻断。
    assert _business_issues([_row(库位号=10104)]) == []


def test_status_empty_blocks_but_non_empty_arbitrary_passes() -> None:
    """状态按「非空」校验（源数据驱动、不穷举 —— 见 validate 模块 docstring）。"""
    assert any("状态不能为空" == i.reason for i in _business_issues([_row(状态="")]))
    # 不在「合格/待检/冻结」内的取值放行（源数据可能含其它状态）。
    assert _business_issues([_row(状态="在途")]) == []


def test_warehouse_mismatch_blocks() -> None:
    issues = _business_issues([_row(仓库号="OTHER")])
    assert any("仓库号须为 GTJ10036" == i.reason for i in issues)


def test_issue_carries_file_column_row() -> None:
    """回执可展开到「文件 + 列 + 行 + 原因」级。"""
    issues = _business_issues([_row(批号="")])
    issue = next(i for i in issues if i.reason == "批号不能为空")
    assert (issue.filename, issue.column, issue.row, issue.layer) == ("INV.csv", "batch_no", 1, ValidationLayer.BUSINESS)


# ------------------------------------------------------------------ 结构 / 字段 / 时点层

def test_structure_empty_blocks() -> None:
    issues = validate_structure([], filename="INV.csv")
    assert issues and issues[0].level is IssueLevel.BLOCKING
    assert "无数据行" in issues[0].reason


def test_fields_missing_required_blocks() -> None:
    """缺「仓库号」→ 字段层阻断；品名缺失 → 降级不阻断。"""
    headers = ["库位号", "料号", "批号", "状态", "数量", "库存记录时间"]  # 无 仓库号、无 品名
    issues = validate_fields(_mapping(headers), file_type=FileType.INV, filename="INV.csv")

    blocking = [i for i in issues if i.level is IssueLevel.BLOCKING]
    degraded = [i for i in issues if i.level is IssueLevel.DEGRADED]
    assert any(i.reason == "缺失必填列：warehouse_no" for i in blocking)
    assert any("品名" in i.reason for i in degraded)


def test_time_missing_blocks() -> None:
    issues = validate_time(None, filename="INV.csv", now=TODAY)
    assert issues and "数据时点为必填" in issues[0].reason


def test_time_future_blocks() -> None:
    issues = validate_time(datetime(2026, 9, 15, 0, 0), filename="INV.csv", now=TODAY)
    assert issues and "晚于当天" in issues[0].reason


def test_time_same_day_is_allowed() -> None:
    assert validate_time(datetime(2026, 9, 14, 23, 59), filename="INV.csv", now=TODAY) == []


# ------------------------------------------------------------------ 综合：降级不阻断

def test_material_name_missing_is_degraded_not_blocking() -> None:
    """选填品名缺失：report.passed 仍为真，回执含降级条目。"""
    headers = ["仓库号", "库位号", "料号", "批号", "状态", "数量", "库存记录时间"]  # 无 品名
    row = {k: v for k, v in _row().items() if k != "品名"}
    report = validate_rows(
        rows=[row], mapping=_mapping(headers), file_type=FileType.INV,
        data_time=DATA_TIME, filename="INV.csv", now=TODAY,
    )
    assert report.passed is True
    assert report.degraded and "品名" in report.degraded[0].reason
