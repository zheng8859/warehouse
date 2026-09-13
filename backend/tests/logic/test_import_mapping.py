"""GTJ10036 字段映射的契约测试（tasks.md 2.3 的验证）。

事实来源：16 §4.2（字段映射；别名/去空格/全半角/大小写容错）
          spec `data-import`「文件解析与字段映射」（Scenario：字段未 100% 命中阻断）
          openspec/changes/data-import/design.md D8

映射是**纯函数**：不建库、不解析文件，只喂表头字符串。要钉住四处容错（别名 / 去空格 /
全半角 / 大小写）与一处阻断（必填列未 100% 命中），以及品名这个选填列「缺失不阻断」。
"""
from __future__ import annotations

import pytest

from app.core.enums import FileType
from app.core.errors import DomainError, ValidationBlocked
from app.importer.mapping import (
    MappingResult,
    build_mapping,
    ensure_complete,
    normalize_header,
)

pytestmark = pytest.mark.logic

INV_HEADERS = ["仓库号", "库位号", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
PO_HEADERS = ["单据号码", "行号", "类型", "仓库号", "料号", "品名", "生产日期", "数量"]


# ------------------------------------------------------------------ 归一

def test_normalize_header_fullwidth_to_halfwidth() -> None:
    """全角拉丁 / 全角括号都要归成半角，否则对不上半角模版。"""
    assert normalize_header("ｌｏｃａｔｉｏｎ") == "location"
    assert normalize_header("数量（箱）") == "数量(箱)"


def test_normalize_header_case_insensitive() -> None:
    assert normalize_header("LOCATION") == "location"
    assert normalize_header("Location") == "location"


def test_normalize_header_strips_whitespace() -> None:
    assert normalize_header("  库位号  ") == "库位号"


def test_normalize_header_none_and_empty() -> None:
    assert normalize_header(None) == ""
    assert normalize_header("   ") == ""


# ------------------------------------------------------------------ 完整命中

def test_inv_full_template_maps_completely() -> None:
    result = build_mapping(INV_HEADERS, FileType.INV)
    assert result.is_complete is True
    assert result.missing_required == frozenset()
    assert result.field_to_source["location_code"] == "库位号"
    assert result.field_to_source["material_code"] == "料号"
    assert result.field_to_source["snapshot_time"] == "库存记录时间"


def test_po_full_template_maps_completely() -> None:
    result = build_mapping(PO_HEADERS, FileType.PO)
    assert result.is_complete is True
    assert result.field_to_source["order_no"] == "单据号码"
    assert result.field_to_source["order_type"] == "类型"
    assert result.field_to_source["production_date"] == "生产日期"


def test_do_has_same_required_set_as_po() -> None:
    """DO 模版与 PO 同构（单据号码/行号/类型/仓库号/料号/品名/生产日期/数量）。"""
    result = build_mapping(PO_HEADERS, FileType.DO)
    assert result.is_complete is True


def test_po_required_is_five_strong_required_only() -> None:
    """PO 强必填只有 5 列；类型（order_type）与生产日期是选填，缺失不阻断。"""
    headers = ["单据号码", "行号", "仓库号", "料号", "数量"]  # 无 类型、无 品名、无 生产日期
    result = build_mapping(headers, FileType.PO)
    assert result.is_complete is True
    assert result.missing_required == frozenset()
    assert "order_type" not in result.field_to_source
    assert "production_date" not in result.field_to_source


# ------------------------------------------------------------------ 容错：别名 / 大小写 / 全半角

def test_alias_matches() -> None:
    """`库位` 是 `库位号` 的别名。"""
    headers = ["仓库", "库位", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)
    assert result.field_to_source["warehouse_no"] == "仓库"
    assert result.field_to_source["location_code"] == "库位"


def test_case_and_fullwidth_headers_match() -> None:
    """`LOCATION_CODE`（大小写）与 `ｌｏｃａｔｉｏｎ`（全角）都应命中。"""
    headers = ["仓库号", "LOCATION_CODE", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)
    assert result.field_to_source["location_code"] == "LOCATION_CODE"

    headers = ["仓库号", "ｌｏｃａｔｉｏｎ", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)
    assert result.field_to_source["location_code"] == "ｌｏｃａｔｉｏｎ"


# ------------------------------------------------------------------ 阻断：必填未 100% 命中

def test_missing_required_column_is_reported() -> None:
    """缺「仓库号」→ `missing_required` 报告，`is_complete` 为假。"""
    headers = ["库位号", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)
    assert result.missing_required == {"warehouse_no"}
    assert result.is_complete is False


def test_ensure_complete_blocks_and_lists_missing() -> None:
    """spec Scenario「字段未 100% 命中阻断」：回执显示 `字段命中 n/m` 并列出缺失列。"""
    headers = ["库位号", "料号", "品名", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)

    with pytest.raises(ValidationBlocked) as excinfo:
        ensure_complete(result, file_type=FileType.INV)

    assert isinstance(excinfo.value, DomainError)
    assert excinfo.value.http_status == 422
    assert "字段命中 6/7" in str(excinfo.value)
    assert excinfo.value.detail["missing"] == ["warehouse_no"]


def test_ensure_complete_passes_when_complete() -> None:
    result = build_mapping(INV_HEADERS, FileType.INV)
    ensure_complete(result, file_type=FileType.INV)  # 不抛


# ------------------------------------------------------------------ 选填：品名缺失降级

def test_optional_material_name_missing_is_not_blocking() -> None:
    """品名是选填列，缺失不阻断、不进 `missing_required`。"""
    headers = ["仓库号", "库位号", "料号", "批号", "状态", "数量", "库存记录时间"]
    result = build_mapping(headers, FileType.INV)
    assert result.is_complete is True
    assert "material_name" not in result.field_to_source
    ensure_complete(result, file_type=FileType.INV)  # 不抛


# ------------------------------------------------------------------ 回执辅助

def test_unrecognized_header_is_reported_not_blocked() -> None:
    """匹配不到任何字段的表头落 `unrecognized` 供回执核对，但不阻断。"""
    headers = [*INV_HEADERS, "备注"]
    result = build_mapping(headers, FileType.INV)
    assert result.unrecognized == {"备注"}
    assert result.is_complete is True


def test_duplicate_header_first_wins() -> None:
    """同名字段出现两次，先到先得（`setdefault`），不改判。"""
    headers = ["仓库号", "库位号", "料号", "品名", "批号", "状态", "数量", "库存记录时间", "料号"]
    result = build_mapping(headers, FileType.INV)
    assert result.field_to_source["material_code"] == "料号"
