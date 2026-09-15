"""文件解析成行 + 单元格值归一（**不碰磁盘 / 库，仅内存字节**）。

事实来源：16-数据衔接与 cap 自维护 §4.2（解析，库位号按文本、日期序列号转日期）
          spec `data-import`「文件解析与字段映射」（Scenario：库位号按文本读取）
          openspec/changes/data-import/design.md D2 / D8
          CLAUDE.md §七（库位号按 6 位文本读取，严禁按列序号硬取）

## 职责边界（为什么把「值归一」留成函数而不是在解析里硬做）

- `parse_rows` 把文件解析成「**表头名 → 原始值**」的行。表头驱动而非列序号驱动：
  列怎么换位都不影响结果，这是「严禁按列序号硬取」的结构性保证。
- 库位号「前导 0 不丢」与日期「序列号转日期」是**值级归一**，作为 `as_location_code`
  与 `as_date` 两个原语提供，**不在 `parse_rows` 里对每个数字列套零填充** ——
  数量 40 不该变成 `"000040"`，而解析器此时还不知道哪一列是库位号（那是
  `mapping.py` 表头→字段映射之后才知道的事）。

于是流水线是：`detect` 判格式编码 → `parse_rows` 出原始行 → `mapping` 认列名 →
对已认出的 `库位号` 列调 `as_location_code`、`生产日期` 列调 `as_date`。库位号按
文本这件事，因此只发生在「已知它是库位号」的那一步，而不是解析器对全表瞎猜。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

import openpyxl

from app.importer.detect import Detected, FileFormat, TextEncoding

__all__ = [
    "as_date",
    "as_datetime",
    "as_location_code",
    "parse_rows",
]

#: 日期字符串的两种常见形态（CSV 导出）：`2026-09-08` 与 `2026/09/08`。
_DATE_TEXT_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")

#: 「库存记录时间」的常见文本形态（WMS/SAP 导出）：除分隔符日期外，还含**无分隔符
#: 紧凑日期** `20260915` 与 14 位紧凑日期时间 `20260915000000` —— 这是 GTJ10036
#: 库存快照导出的真实形态，缺了它会在执行分流时把合法文件解析崩（曾致导入页 500）。
_DATETIME_TEXT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)

#: Excel 日期序列号的纪元。serial 1 = 1900-01-01，但有 1900 闰年 bug，故基准是
#: 1899-12-30（`datetime(1899,12,30) + timedelta(days=serial)` 才是正确换算）。
_EXCEL_EPOCH = date(1899, 12, 30)


def parse_rows(content: bytes, detected: Detected) -> list[dict[str, Any]]:
    """把文件解析成行，每行是「表头名 → 原始值」的字典。

    - xlsx：openpyxl 读第一个 sheet，第一行当表头，日期格会以 `datetime` 返回
      （openpyxl 已按 number_format 换算），数值格以 int/float 返回。
    - csv：按探测到的编码解码，`csv.DictReader` 以首行为表头，值一律是字符串。

    返回空列表当且仅当文件无表头或全是空行 —— 「非空」的结构校验在 `validate.py`，
    本函数不越权判空。
    """
    if detected.format is FileFormat.CSV:
        return _parse_csv(content, detected.encoding)
    return _parse_xlsx(content)


def _parse_csv(content: bytes, encoding: TextEncoding) -> list[dict[str, Any]]:
    codec = {
        TextEncoding.UTF_8: "utf-8",
        TextEncoding.UTF_8_BOM: "utf-8-sig",  # 顺带剥掉 BOM，不把 ﻿ 塞进表头
        TextEncoding.GBK: "gbk",
    }[encoding]
    text = content.decode(codec)
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _parse_xlsx(content: bytes) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb.active
        raw_iter = ws.iter_rows(values_only=True)

        headers = next(raw_iter, None)
        if headers is None:
            return []

        rows: list[dict[str, Any]] = []
        for raw in raw_iter:
            if raw is None or all(v is None for v in raw):
                continue  # 跳过整行为空的行
            row = {
                str(header).strip(): value
                for header, value in zip(headers, raw)
                if header is not None and str(header).strip() != ""
            }
            rows.append(row)
        return rows
    finally:
        wb.close()


def as_location_code(value: Any) -> str:
    """库位号按 6 位文本读取：前导 0 不丢。

    Excel 数值化会把 `010104` 存成整数 `10104`（前导 0 丢）。这里按整数归一时补回
    前导 0（`10104 → "010104"`）；本就是字符串（如 `"010104"`）则去首尾空格原样返回。
    非整数浮点 / 布尔 / 空值都抛 `ValueError` —— 那是结构/口径问题，由上层（映射或
    校验）转成回执明细，本函数只负责「值归一」这一个动作。
    """
    if isinstance(value, bool):
        raise ValueError("库位号不能是布尔值")

    if isinstance(value, int):
        return f"{value:06d}"

    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):06d}"
        raise ValueError(f"库位号是非整数浮点：{value}")

    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
        raise ValueError("库位号为空字符串")

    raise ValueError(f"库位号值类型不可归一为文本：{type(value).__name__}")


def as_date(value: Any) -> date | None:
    """日期序列号转日期；缺失返回 `None`（选填降级，不阻断）。

    - `datetime` / `date` → 取日期部分（openpyxl 对日期格直接给 `datetime`）。
    - int / float → Excel 序列号（`datetime(1899,12,30) + timedelta(days=serial)`）。
    - 字符串 → 先试 `YYYY-MM-DD` / `YYYY/MM/DD`，再试纯数字序列号。
    - `None` / 空串 → `None`（缺失，交由上层按「选填」降级）。
    - 其它不可解析 → 抛 `ValueError`（口径异常，上层转回执）。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        raise ValueError("日期不能是布尔值")
    if isinstance(value, (int, float)):
        return _EXCEL_EPOCH + timedelta(days=int(value))

    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        for fmt in _DATE_TEXT_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        if text.isdigit():
            return _EXCEL_EPOCH + timedelta(days=int(text))
        raise ValueError(f"无法解析日期文本：{value!r}")

    raise ValueError(f"日期值类型不可归一：{type(value).__name__}")


def as_datetime(value: Any) -> datetime:
    """库存记录时间归一成 `datetime`（必填列：空值 / 不可解析一律抛 `ValueError`）。

    与 `as_date` 的区别：那是**选填**列（生产日期）缺失返回 `None`；本列必填，缺失与
    解析失败都必须抛错，由校验层转成阻断明细（而不是在执行分流时崩成 500）。

    支持的输入：
      - `datetime` 直取；`date` 补当日零点；
      - 字符串：见 `_DATETIME_TEXT_FORMATS`（含 WMS 导出的无分隔符 `20260915`、
        `20260915000000`）；
      - `int` / `float`：8 位整数按 `%Y%m%d`（Excel 把紧凑日期存成数字格的情形），
        其余按 Excel 日期序列号（与 `as_date` 同一纪元换算）。

    `bool` 显式拒绝（`True` 会被当成 `1` 的历史坑）。
    """
    if isinstance(value, bool):
        raise ValueError("库存记录时间不能是布尔值")

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    if isinstance(value, (int, float)):
        number = int(value)
        # 8 位紧凑日期（YYYYMMDD）：Excel 数字格存 20260915 的情形；序列号恒为 5 位
        # （2026 年约 46xxx），二者不会混淆。先验 8 位、再回退序列号。
        if 19000101 <= number <= 29991231:
            return datetime.strptime(str(number), "%Y%m%d")
        return datetime.combine(_EXCEL_EPOCH, datetime.min.time()) + timedelta(days=number)

    if isinstance(value, str):
        text = value.strip()
        if text == "":
            raise ValueError("库存记录时间为空字符串")
        for fmt in _DATETIME_TEXT_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"无法解析库存记录时间：{value!r}")

    raise ValueError(f"库存记录时间值类型不可归一：{type(value).__name__}")
