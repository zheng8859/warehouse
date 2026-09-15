"""从最新库存快照提取物料主数据（料号 / 品名 / 每板箱数）写入 materials 表。

品名里「N/板」解析为 cartons_per_pallet；品名未标注的从**真实库存**推导（各库位
`qty` 的众数，即满板箱数，D14「1 板 = 1 格」）；两者皆无则退化为 None（引擎兜底过估）。
ABC 分类不在本脚本范围（需销售频次分析，属 ABC 分类导入流程）。

用法：
    python scripts/extract_materials.py
    python scripts/extract_materials.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from collections import Counter  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.linkage import InventoryItem, Snapshot  # noqa: E402
from app.models.master_data import Material  # noqa: E402

_PALLET_RE = re.compile(r"(\d+)\s*/\s*板")


def _parse_cartons_per_pallet(name: str | None) -> int | None:
    if not name:
        return None
    m = _PALLET_RE.search(name)
    return int(m.group(1)) if m else None


def _derive_cartons_per_pallet(
    session, *, snapshot_id: int, warehouse_id: str, material_code: str
) -> int | None:
    """品名未标注「N/板」时，从**真实库存**推导每板箱数：该料号当前快照各库位 `qty` 的众数。

    依据 D14（2026-09-15 确认）「1 板 = 1 格、qty 单位是箱」：一个库位 = 一格 = 一板，
    满板库位的 `qty` 即每板箱数，故「各库位 qty 的众数」就是满板箱数（满板库位最常见）。
    只当众数有 **≥2 个库位同值**才采信 —— 单库位信号太弱，宁可退回 `None` 走引擎的保守
    过估（一箱一格），也不把一个孤证当满板箱数。这是从真实库存行推导，不是编造常量。
    """
    qty_counts = Counter(
        qty
        for (qty,) in session.execute(
            select(InventoryItem.qty).where(
                InventoryItem.snapshot_id == snapshot_id,
                InventoryItem.warehouse_id == warehouse_id,
                InventoryItem.material_code == material_code,
            )
        )
    )
    if not qty_counts:
        return None
    (mode, count), = qty_counts.most_common(1)
    if count >= 2 and mode > 0:
        return mode
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从库存快照提取物料主数据")
    parser.add_argument("--warehouse", default=settings.warehouse_code)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        # 取最新快照的库存行去重料号 + 品名
        latest = session.scalars(
            select(Snapshot).where(Snapshot.warehouse_id == args.warehouse)
            .order_by(Snapshot.version_no.desc()).limit(1)
        ).first()
        if latest is None:
            print("[extract_materials] 无快照，退出")
            return 1

        rows = session.execute(
            select(
                InventoryItem.material_code,
                InventoryItem.material_name,
            )
            .where(InventoryItem.snapshot_id == latest.id)
            .group_by(InventoryItem.material_code, InventoryItem.material_name)
        ).all()

        parsed = []
        for code, name in rows:
            cpp = _parse_cartons_per_pallet(name)
            if cpp is None:
                cpp = _derive_cartons_per_pallet(
                    session,
                    snapshot_id=latest.id,
                    warehouse_id=args.warehouse,
                    material_code=code,
                )
            parsed.append((code, name, cpp))

        print(f"[extract_materials] 快照 v{latest.version_no} 提取 {len(parsed)} 个料号")
        with_cpp = [p for p in parsed if p[2] is not None]
        print(f"  品名含「N/板」可解析每板箱数: {len(with_cpp)}")
        print(f"  品名未标注（cartons_per_pallet=None）: {len(parsed) - len(with_cpp)}")
        if with_cpp:
            dist = Counter(p[2] for p in with_cpp)
            print(f"  每板箱数分布: {dict(dist.most_common(10))}")

        if args.dry_run:
            for code, name, cpp in parsed[:10]:
                print(f"    {code}  cpp={cpp}  {name}")
            print("..." if len(parsed) > 10 else "")
            return 0

        existing = {
            m.material_code: m
            for m in session.scalars(
                select(Material).where(Material.warehouse_id == args.warehouse)
            )
        }
        inserted = updated = 0
        for code, name, cpp in parsed:
            row = existing.get(code)
            if row is None:
                session.add(Material(
                    warehouse_id=args.warehouse,
                    material_code=code,
                    material_name=name,
                    cartons_per_pallet=cpp,
                ))
                inserted += 1
            else:
                row.material_name = name or row.material_name
                row.cartons_per_pallet = cpp
                updated += 1
        session.commit()
        print(f"[extract_materials] upsert 完成：新增 {inserted}，更新 {updated}")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
