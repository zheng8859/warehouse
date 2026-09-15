"""生产批号生成（D11）的纯函数测试。

事实来源：openspec/changes/data-import/design.md D11（批次号的现场日期）
          app/core/clock.py（`require_wall_clock` —— 批次号与预留池释放共用同一判据）

钉住三点：

1. **格式**：10 位 = 公司别 3 + 年 2 + 月 1 + 日 2 + 线 1 + 班 1，例 2026-09-15 →
   `GJP2691571`。
2. **月份溢出编码**：10/11/12 → A/B/C，其余月份用数字本身（不是十六进制）。
3. **守卫**：`now` 带时区抛 `TypeError`（D17 —— 现场墙上时间，朴素）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.importer.batch_no import generate_batch_no


def test_normal_date_encodes_yy_m_dd_line_shift() -> None:
    """2026-09-15 → GJP2691571：年 2 码、月 1 码、日补零、线 7、班 1。"""
    assert generate_batch_no(datetime(2026, 9, 15, 14, 30)) == "GJP2691571"


def test_single_digit_month_and_day_are_padded_or_kept() -> None:
    """月 1 位不补零、日 2 位补零：2026-01-05 → GJP2610571。"""
    assert generate_batch_no(datetime(2026, 1, 5)) == "GJP2610571"


def test_oct_nov_dec_encode_as_abc() -> None:
    """10/11/12 月溢出到字母 A/B/C，不是十六进制的 0/1/2。"""
    assert generate_batch_no(datetime(2026, 10, 3)) == "GJP26A0371"
    assert generate_batch_no(datetime(2026, 11, 22)) == "GJP26B2271"
    assert generate_batch_no(datetime(2026, 12, 31)) == "GJP26C3171"


def test_year_is_two_digit_mod_100() -> None:
    """年份取 2 码（% 100）：2026 → 26，2000 → 00。"""
    assert generate_batch_no(datetime(2026, 9, 15)).startswith("GJP26")
    assert generate_batch_no(datetime(2000, 9, 15)).startswith("GJP00")


def test_company_line_shift_are_overrideable() -> None:
    """公司别 / 线 / 班可经关键字参数覆盖（多厂多线扩展）。"""
    assert generate_batch_no(datetime(2026, 9, 15), company="GTJ", line="3", shift="2") == "GTJ2691532"


def test_tz_aware_now_raises_typeerror() -> None:
    """`now` 带时区 → TypeError（D17 守卫：批号读的是现场墙上时间）。"""
    with pytest.raises(TypeError):
        generate_batch_no(datetime(2026, 9, 15, tzinfo=timezone.utc))
