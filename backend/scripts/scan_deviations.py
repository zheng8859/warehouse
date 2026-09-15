"""全量偏离扫描脚本：`scan_material_deviations` 的手动补扫 / 回填入口。

用法（cwd 不限，脚本自己把 backend/ 放进 sys.path）：

    python scripts/scan_deviations.py                  # 扫当前最新快照
    python scripts/scan_deviations.py --warehouse GTJ10036

扫描**当前最新快照**，把「同物料跨巷道 > 5」的物料落成物料级 `Deviation`（幂等只补缺）。
导入流程已自动接此扫描（`importer/execute.py`），本脚本用于：

1. **回填既有快照**：代码上线前已导入的历史快照不会自动补扫；
2. **漂移校正后手动补扫**：与 `recompute_snapshot_caps` 同一精神，改了快照后补扫偏离。

不建基线、不进 `ImportSession` 状态机、不产 `JobOrder`。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本脚本在 backend/scripts/ 下；把 backend/ 提前放进 sys.path，
# 这样无论从哪个 cwd 调起，`import app.*` 都能解析。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.cap.deviation import scan_material_deviations  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.linkage import Snapshot  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="全量偏离扫描（回填/补扫当前快照）")
    parser.add_argument("--warehouse", default=settings.warehouse_code, help="单厂仓库号")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    session = SessionLocal()
    try:
        snapshot = session.scalars(
            select(Snapshot)
            .where(Snapshot.warehouse_id == args.warehouse)
            .order_by(Snapshot.version_no.desc(), Snapshot.id.desc())
            .limit(1)
        ).first()
        if snapshot is None:
            print(f"[scan_deviations] 仓库 {args.warehouse} 无快照基线，跳过")
            return 0

        created = scan_material_deviations(
            session, warehouse_id=args.warehouse, snapshot_id=snapshot.id
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(
        f"[scan_deviations] 快照 #{snapshot.id}（版本 {snapshot.version_no}）"
        f"扫描完成：新增物料级偏离 {len(created)} 条"
    )
    for dev in created:
        print(f"[scan_deviations] 物料 {dev.material_code} 跨巷道 {dev.actual_cross_aisle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
