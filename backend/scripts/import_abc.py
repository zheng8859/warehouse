"""成品清单 ABC 统计导入脚本（`16` A.4.1 · design.md D6）。

用法（cwd 不限，脚本自己把 backend/ 放进 sys.path）：

    python scripts/import_abc.py                            # 默认路径 / 默认 sheet
    python scripts/import_abc.py --path <xlsx> --sheet <名>

读 BI 看板需求「成品清单」sheet（`openpyxl` read_only 迭代，不整表载入内存），按料号聚合
出库量（`移动类型 == 出库` 的 `数量` 求和），降序累计占比分档（A≤70% / B≤90% / C 其余），
upsert 到 `Material.abc_class`；料号不在主数据 → 告警跳过（不阻断）。幂等（重复跑覆盖），
**不建基线、不进 `ImportSession` 状态机、不产 `JobOrder`**。

## 与三类作业文件导入的分界

这是**脚本通道**，不是 p2 页面交互、不走 `/api/import/*`。顺序约束（design.md D6）：
至少一次 INV/PO/DO 导入之后跑 —— `Material` 行由文件导入携带「料号 + 品名」，缺失时
本脚本告警跳过，`abc_class` 落不全；主数据补录是另一关注点，不在本脚本内解决。

## 列名按表头识别（不按列序号）

「料号 / 移动类型 / 数量」三列按**表头名**定位，不是固定列号（CLAUDE.md 红线「严禁按列
序号硬取字段」）。BI 导出可能在表头前有标题行，故先扫前 `_HEADER_SCAN_ROWS` 行找表头
（一行里能同时认到三个目标列即为表头），再从下一行起聚合。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# 本脚本在 backend/scripts/ 下；把 backend/ 提前放进 sys.path，
# 这样无论从哪个 cwd 调起，`import app.*` 与 `import scripts.*` 都能解析。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.enums import AbcClass  # noqa: E402
from app.importer.abc import apply_abc_classes, classify_abc  # noqa: E402

#: 默认源文件路径（design.md D6）。单厂内置，可 `--path` 覆盖。
DEFAULT_PATH = r"D:\成品库位智能推荐\BI看板需求 -GTJ10036.xlsx"

#: 默认 sheet 名（design.md D6：「成品清单」sheet）。
DEFAULT_SHEET = "成品清单"

#: 出库移动类型（design.md D6：`移动类型 == 出库` 的数量求和）。
OUTBOUND_MOVE_TYPE = "出库"

#: 表头扫描上限：在表头前最多容忍这么多行标题/说明行。
_HEADER_SCAN_ROWS = 20

#: 三个目标列的表头别名（全半角/大小写归一后匹配）。只认这些，不引入配置。
_COLUMN_ALIASES: dict[str, frozenset[str]] = {
    "material_code": frozenset({"料号", "物料号", "物料编码", "物料编号"}),
    "move_type": frozenset({"移动类型", "移动类型名称", "单据类型"}),
    "qty": frozenset({"数量", "库存数量", "出库数量"}),
}


def _normalize(value: Any) -> str:
    """表头归一：去首尾空格 + 大小写不敏感（与 `mapping.normalize_header` 同精神）。"""
    if value is None:
        return ""
    return str(value).strip().casefold()


def _resolve_header(row: tuple[Any, ...]) -> dict[str, int] | None:
    """在一行里定位三列，返回 {内部名: 列索引}；三列不齐返回 `None`。"""
    indices: dict[str, int] = {}
    for col_no, cell in enumerate(row):
        norm = _normalize(cell)
        for field, aliases in _COLUMN_ALIASES.items():
            if field not in indices and norm in aliases:
                indices[field] = col_no
    return indices if set(indices) == set(_COLUMN_ALIASES) else None


def _find_header(rows: Iterable[tuple[Any, ...]]) -> dict[str, int] | None:
    """扫前 `_HEADER_SCAN_ROWS` 行找表头（一行能同时认到三列即为表头）。"""
    for _, row in zip(range(_HEADER_SCAN_ROWS), rows):
        header = _resolve_header(row)
        if header is not None:
            return header
    return None


def _as_code(value: Any) -> str:
    """料号归一为文本：整数浮点（Excel 数值化）去小数尾巴，前导/尾随空格去掉。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_int(value: Any) -> int | None:
    """数量归一为 int；不可归一返回 `None`（供调用方告警跳过该行）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _aggregate_outbound(
    rows: Iterable[tuple[Any, ...]], header: dict[str, int]
) -> tuple[dict[str, int], int]:
    """按料号聚合出库量：`移动类型 == 出库` 的 `数量` 求和。

    返回 `(totals, skipped_rows)`：`skipped_rows` 是数量不可归一 / 料号为空的行数
    （告警，不阻断 —— 与「料号缺失告警跳过」同一口径）。
    """
    totals: dict[str, int] = {}
    skipped_rows = 0
    for row in rows:
        if _normalize(row[header["move_type"]]) != _normalize(OUTBOUND_MOVE_TYPE):
            continue
        code = _as_code(row[header["material_code"]])
        qty = _as_int(row[header["qty"]])
        if not code or qty is None:
            skipped_rows += 1
            continue
        totals[code] = totals.get(code, 0) + qty
    return totals, skipped_rows


def _read_outbound_totals(path: str, sheet: str) -> tuple[dict[str, int], int]:
    """读 xlsx「成品清单」sheet，聚合出库量。返回 `(totals, skipped_rows)`。"""
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise ValueError(f"xlsx 里没有 sheet「{sheet}」；现有：{workbook.sheetnames}")
        worksheet = workbook[sheet]
        rows = worksheet.iter_rows(values_only=True)
        header = _find_header(rows)
        if header is None:
            raise ValueError(
                f"「{sheet}」前 {_HEADER_SCAN_ROWS} 行内未找到表头"
                "（需同时含 料号 / 移动类型 / 数量 三列）"
            )
        return _aggregate_outbound(rows, header)
    finally:
        workbook.close()


def _count_by_class(classes: dict[str, AbcClass]) -> dict[str, int]:
    counts = {c.value: 0 for c in AbcClass}
    for cls in classes.values():
        counts[cls.value] += 1
    return counts


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="成品清单 ABC 统计导入（脚本通道）")
    parser.add_argument("--path", default=DEFAULT_PATH, help="BI 看板需求 xlsx 路径")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="成品清单 sheet 名")
    parser.add_argument("--warehouse", default=settings.warehouse_code, help="单厂仓库号")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    totals, skipped_rows = _read_outbound_totals(args.path, args.sheet)
    print(f"[import_abc] 聚合出库量：{len(totals)} 个料号（{skipped_rows} 行口径异常跳过）")
    if not totals:
        print("[import_abc] 无有出库量的料号，跳过 upsert")
        return 0

    classes = classify_abc(totals)
    counts = _count_by_class(classes)
    print(f"[import_abc] 分档：A {counts['A']} / B {counts['B']} / C {counts['C']}")

    session = SessionLocal()
    try:
        result = apply_abc_classes(session, warehouse_id=args.warehouse, classes=classes)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(f"[import_abc] upsert：更新 {result.updated}，跳过 {len(result.skipped)}")
    for code in result.skipped:
        print(f"[import_abc] 告警：料号 {code} 不在主数据，跳过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
