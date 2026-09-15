"""巷道物理总格数（`Aisle.total_cells`）历史观测推导脚本。

用法（cwd 不限，脚本自己把 backend/ 放进 sys.path）：

    python scripts/derive_aisle_cells.py
    python scripts/derive_aisle_cells.py --path <xlsx> --near 01,02

## 为什么需要它

巷道物理总格数是 cap 口径的分母（`cap_total = total_cells − 已占格数`），权威值应来自
WMS 巷道主数据导出；该导出到位前没有权威值。本脚本用 `BI看板需求 -GTJ10036.xlsx` 的
**历史库位并集**给出可复现的**下限近似**：

- 「成品清单」sheet（44 万+ 出入库/移库动作）的「移动库位号 / 目的库位号」
- 「库存记录表」sheet 的「库位号」
- 同一巷道历史上**出现过的去重 6 位库位**并集大小 ≈ 该巷道物理总格数

证据强度：16 个月、单巷 4 千+ 次动作、各巷并集高度一致（729~751）——高周转立体库里
长期未被轮换到的格极少，并集已逼近物理全量。**它仍是观测下限，不是图纸权威值**；将来
WMS 导出了权威总格数，用导出值覆盖本脚本结果即可（脚本幂等，可重复跑）。

同时按 `--near`（默认 `01,02`，依据 14 §2.2 + 移库归集集中在低编号巷道）写
`Aisle.is_near_station`，其余巷道写 `False`。**不建库位主数据、不建快照、不碰 cap**。

## 列名按表头识别（不按列序号）

与 `import_abc.py` 同一红线：扫前 `_HEADER_SCAN_ROWS` 行找表头，按表头名定位列。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# 本脚本在 backend/scripts/ 下；把 backend/ 提前放进 sys.path。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.master_data import Aisle  # noqa: E402

#: 默认源文件路径（与 import_abc.py 同源）。
DEFAULT_PATH = r"D:\成品库位智能推荐\BI看板需求 -GTJ10036.xlsx"

#: 默认近站台巷道（14 §2.2）。
DEFAULT_NEAR = "01,02"

#: 表头扫描上限（与 import_abc.py 一致）。
_HEADER_SCAN_ROWS = 20

#: 各 sheet 需要的库位列表头别名。
_MOVE_SHEET = "成品清单"
_MOVE_LOC_COLUMNS = ("移动库位号", "目的库位号")
_STOCK_SHEET = "库存记录表"
_STOCK_LOC_COLUMN = "库位号"


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().casefold()


def _as_location(value: Any) -> str | None:
    """库位号归一为 6 位数字文本；非法（空 / 非数字 / 超出 6 位）返回 `None`。

    Excel 会把 `010104` 数值化成 `10104`、把 `000000` 数值化成 `0` —— 纯数字时左侧
    补 0 到 6 位还原（真实库位均为 6 位，`16` §4.2）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    text = str(value).strip()
    if not text.isdigit() or len(text) > 6:
        return None
    return text.zfill(6)


def _find_columns(row: tuple[Any, ...], targets: Iterable[str]) -> dict[str, int] | None:
    """在一行里按表头名定位目标列，全部找到才返回。"""
    wanted = {_normalize(t) for t in targets}
    found: dict[str, int] = {}
    for col_no, cell in enumerate(row):
        norm = _normalize(cell)
        if norm in wanted and norm not in found:
            found[norm] = col_no
    return found if wanted <= set(found) else None


def _collect_from_sheet(worksheet, columns: Iterable[int]) -> set[str]:
    """流式扫描一个 sheet，把指定列里的合法 6 位库位收进集合。"""
    locations: set[str] = set()
    for row in worksheet.iter_rows(values_only=True):
        for col_no in columns:
            loc = _as_location(row[col_no] if col_no < len(row) else None)
            if loc is not None:
                locations.add(loc)
    return locations


def collect_historical_locations(path: str) -> dict[str, set[str]]:
    """读两个 sheet，返回 {巷道号: {历史库位...}}（巷道 = 库位号前 2 位）。"""
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    locations: set[str] = set()
    try:
        # 1) 成品清单：移动库位号 + 目的库位号（表头可能有标题行，先扫表头）
        if _MOVE_SHEET not in workbook.sheetnames:
            raise ValueError(f"xlsx 里没有 sheet「{_MOVE_SHEET}」：{workbook.sheetnames}")
        ws_move = workbook[_MOVE_SHEET]
        rows = ws_move.iter_rows(values_only=True)
        header: dict[str, int] | None = None
        for _, row in zip(range(_HEADER_SCAN_ROWS), rows):
            header = _find_columns(row, _MOVE_LOC_COLUMNS)
            if header is not None:
                break
        if header is None:
            raise ValueError(f"「{_MOVE_SHEET}」前 {_HEADER_SCAN_ROWS} 行未找到库位列表头")
        col_indices = [header[_normalize(c)] for c in _MOVE_LOC_COLUMNS]
        locations |= _collect_from_sheet(ws_move, col_indices)  # rows 已是表头之后的迭代器

        # 2) 库存记录表：库位号
        if _STOCK_SHEET in workbook.sheetnames:
            ws_stock = workbook[_STOCK_SHEET]
            rows2 = ws_stock.iter_rows(values_only=True)
            header2: dict[str, int] | None = None
            for _, row in zip(range(_HEADER_SCAN_ROWS), rows2):
                header2 = _find_columns(row, [_STOCK_LOC_COLUMN])
                if header2 is not None:
                    break
            if header2 is not None:
                locations |= _collect_from_sheet(
                    ws_stock, [header2[_normalize(_STOCK_LOC_COLUMN)]]
                )
    finally:
        workbook.close()

    by_aisle: dict[str, set[str]] = defaultdict(set)
    for loc in locations:
        by_aisle[loc[:2]].add(loc)
    return dict(by_aisle)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="巷道物理总格数历史观测推导（脚本通道）")
    parser.add_argument("--path", default=DEFAULT_PATH, help="BI 看板需求 xlsx 路径")
    parser.add_argument("--warehouse", default=settings.warehouse_code, help="单厂仓库号")
    parser.add_argument(
        "--near",
        default=DEFAULT_NEAR,
        help="近站台巷道，逗号分隔两位编号（默认 01,02；其余写 False）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印推导结果，不写库",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    near_aisles = {a.strip().zfill(2) for a in args.near.split(",") if a.strip()}

    by_aisle = collect_historical_locations(args.path)
    print(f"[derive_aisle_cells] 历史观测巷道 {len(by_aisle)} 条，近站台：{sorted(near_aisles)}")
    for aisle_no in sorted(by_aisle):
        print(f"  {aisle_no}: total_cells ≈ {len(by_aisle[aisle_no])}"
              f"{'（近站台）' if aisle_no in near_aisles else ''}")

    if args.dry_run:
        print("[derive_aisle_cells] --dry-run，未写库")
        return 0

    session = SessionLocal()
    try:
        existing = {
            a.aisle_no: a
            for a in session.scalars(
                select(Aisle).where(Aisle.warehouse_id == args.warehouse)
            )
        }
        inserted = updated = 0
        for aisle_no in sorted(by_aisle):
            total_cells = len(by_aisle[aisle_no])
            near = aisle_no in near_aisles
            row = existing.get(aisle_no)
            if row is None:
                session.add(
                    Aisle(
                        warehouse_id=args.warehouse,
                        aisle_no=aisle_no,
                        total_cells=total_cells,
                        is_near_station=near,
                    )
                )
                inserted += 1
            else:
                row.total_cells = total_cells
                row.is_near_station = near
                updated += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(f"[derive_aisle_cells] upsert 完成：新增 {inserted}，更新 {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
