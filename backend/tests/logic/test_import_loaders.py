"""文件解析与单元格值归一的契约测试（tasks.md 2.2 的验证）。

事实来源：16 §4.2（解析；库位号按文本、日期序列号转日期）
          spec `data-import`「文件解析与字段映射」（Scenario：库位号按文本读取）
          CLAUDE.md §七（库位号按 6 位文本读取，严禁按列序号硬取）

解析只碰**内存字节**：本文件不连库、不建磁盘临时文件，xlsx 用 openpyxl 造到
`io.BytesIO` 再喂回 `parse_rows`。核心要钉住两件事：库位号的前导 0 不丢（Excel 数值化
会把 `010104` 存成 `10104`），以及日期序列号转成 `date`（不是把 45713 当数字漏下去）。
"""
from __future__ import annotations

import io
from datetime import date, datetime

import openpyxl
import pytest

from app.importer.detect import Detected, FileFormat, TextEncoding
from app.importer.loaders import as_date, as_datetime, as_location_code, parse_rows

pytestmark = pytest.mark.logic


def _make_xlsx(headers: list, rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ parse_rows：csv

def test_parse_csv_keys_by_header_not_position() -> None:
    """列换位后结果不变 —— 表头驱动，不依赖列序号（「严禁按列序号硬取」）。"""
    content_a = "料号,品名,数量\nM1,可乐,40\n".encode("utf-8")
    content_b = "数量,品名,料号\n40,可乐,M1\n".encode("utf-8")

    rows_a = parse_rows(content_a, Detected(FileFormat.CSV, TextEncoding.UTF_8))
    rows_b = parse_rows(content_b, Detected(FileFormat.CSV, TextEncoding.UTF_8))

    assert rows_a == rows_b == [{"料号": "M1", "品名": "可乐", "数量": "40"}]


def test_parse_csv_gbk_decodes() -> None:
    """GBK 编码的 csv 必须按探测到的编码解，否则中文变乱码。"""
    content = "料号,品名\nM1,可乐\n".encode("gbk")
    rows = parse_rows(content, Detected(FileFormat.CSV, TextEncoding.GBK))
    assert rows == [{"料号": "M1", "品名": "可乐"}]


def test_parse_csv_bom_stripped_from_header() -> None:
    """BOM 不得混进第一个表头名 —— 否则映射找不到「料号」列。"""
    content = b"\xef\xbb\xbf" + "料号,品名\nM1,可乐\n".encode("utf-8")
    rows = parse_rows(content, Detected(FileFormat.CSV, TextEncoding.UTF_8_BOM))
    assert list(rows[0].keys())[0] == "料号"


# ------------------------------------------------------------------ parse_rows：xlsx

def test_parse_xlsx_typed_values() -> None:
    """xlsx 解析：日期格给 datetime、数值格给 int、文本格给 str（openpyxl 已换算）。"""
    content = _make_xlsx(
        ["料号", "品名", "生产日期", "数量"],
        [["M1", "可乐", datetime(2026, 9, 8, 0, 0), 40]],
    )
    rows = parse_rows(content, Detected(FileFormat.XLSX, None))
    assert rows == [
        {"料号": "M1", "品名": "可乐", "生产日期": datetime(2026, 9, 8, 0, 0), "数量": 40}
    ]


def test_parse_xlsx_skips_empty_rows() -> None:
    content = _make_xlsx(
        ["料号", "品名"],
        [["M1", "可乐"], [None, None], ["M2", "雪碧"]],
    )
    rows = parse_rows(content, Detected(FileFormat.XLSX, None))
    assert [r["料号"] for r in rows] == ["M1", "M2"]


def test_parse_xlsx_empty_header_row_returns_empty() -> None:
    content = _make_xlsx([], [])
    assert parse_rows(content, Detected(FileFormat.XLSX, None)) == []


# ------------------------------------------------------------------ as_location_code：前导 0 不丢

def test_location_int_recovers_leading_zero() -> None:
    """Excel 数值化把 `010104` 存成 `10104` —— 归一补回前导 0。"""
    assert as_location_code(10104) == "010104"


def test_location_float_recovers_leading_zero() -> None:
    """CSV 里可能带小数点的数值串被转成 float，同样要补回。"""
    assert as_location_code(10104.0) == "010104"


def test_location_text_unchanged() -> None:
    """本就按文本读的库位号原样返回（只去首尾空格）。"""
    assert as_location_code("010104") == "010104"
    assert as_location_code(" 010104 ") == "010104"


@pytest.mark.parametrize("bad", [None, True, 10104.5, "", "  "])
def test_location_invalid_value_raises(bad: object) -> None:
    """空值 / 布尔 / 非整数浮点都不是合法库位号 —— 归一阶段就拦下，不往下渗。"""
    with pytest.raises(ValueError):
        as_location_code(bad)  # type: ignore[arg-type]


# ------------------------------------------------------------------ as_date：序列号转日期

def test_date_serial_to_date() -> None:
    """Excel 序列号 45713 = 2025-02-25（与 openpyxl 的 `from_excel` 逐位一致）。"""
    assert as_date(45713) == date(2025, 2, 25)


def test_date_datetime_takes_date_part() -> None:
    assert as_date(datetime(2026, 9, 8, 23, 59)) == date(2026, 9, 8)


def test_date_text_formats() -> None:
    assert as_date("2026-09-08") == date(2026, 9, 8)
    assert as_date("2026/09/08") == date(2026, 9, 8)


def test_date_text_serial() -> None:
    """CSV 导出可能把日期写成纯数字序列号文本。"""
    assert as_date("45713") == date(2025, 2, 25)


def test_date_missing_is_none() -> None:
    """选填日期缺失 → None（降级，不阻断）。"""
    assert as_date(None) is None
    assert as_date("") is None
    assert as_date("   ") is None


def test_date_garbage_raises() -> None:
    with pytest.raises(ValueError):
        as_date("not-a-date")


# ------------------------------------------------------------------ as_datetime：库存记录时间归一

def test_datetime_compact_yyyymmdd_text() -> None:
    """WMS 导出的无分隔符 `20260915` 必须能解析（本次导入页 500 的真实根因）。"""
    assert as_datetime("20260915") == datetime(2026, 9, 15)


def test_datetime_compact_yyyymmddhhmmss_text() -> None:
    """14 位紧凑日期时间。"""
    assert as_datetime("20260915083000") == datetime(2026, 9, 15, 8, 30, 0)


def test_datetime_compact_yyyymmdd_int() -> None:
    """Excel 数字格把紧凑日期存成 int 20260915（非 Excel 序列号，序列号 2026 年约 46xxx）。"""
    assert as_datetime(20260915) == datetime(2026, 9, 15)


def test_datetime_serial_int_still_works() -> None:
    """5 位整数仍是 Excel 序列号（45713 = 2025-02-25），不能被 8 位分支误吞。"""
    assert as_datetime(45713) == datetime(2025, 2, 25)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-09-15 00:00:00", datetime(2026, 9, 15, 0, 0, 0)),
        ("2026-09-15 08:30", datetime(2026, 9, 15, 8, 30)),
        ("2026-09-15", datetime(2026, 9, 15)),
        ("2026/09/15 08:30:00", datetime(2026, 9, 15, 8, 30, 0)),
        ("2026/09/15", datetime(2026, 9, 15)),
    ],
)
def test_datetime_separated_text_formats(text: str, expected: datetime) -> None:
    assert as_datetime(text) == expected


def test_datetime_datetime_and_date_passthrough() -> None:
    assert as_datetime(datetime(2026, 9, 15, 7, 57)) == datetime(2026, 9, 15, 7, 57)
    assert as_datetime(date(2026, 9, 15)) == datetime(2026, 9, 15, 0, 0)


@pytest.mark.parametrize("bad", [None, "", "   ", "not-a-time", True, False])
def test_datetime_invalid_raises(bad: object) -> None:
    """必填列：空值 / 不可解析 / 布尔都抛 ValueError，由校验层转阻断、执行层落 FAILED。"""
    with pytest.raises((ValueError, TypeError)):
        as_datetime(bad)  # type: ignore[arg-type]
