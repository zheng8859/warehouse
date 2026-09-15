"""移库完成后的偏离回填脚本：把「已发起移库 → 已改善」推进补到已执行完的移库单上。

事实来源：`15-04` §4.1 第 7 步「写移库台账 + 后验刷新」产出「台账 + KPI 刷新」。
`app/services/verify.py` 的 `_reconcile_relocate_deviation` 在**新发生的**移库后验里
推进这条状态；本脚本补的是**上线前**已 VERIFIED、但状态仍停在「已发起移库」的存量
物料级偏离 —— 用最新快照重算同物料跨巷道数，刷新 `actual_cross_aisle`，收拢到阈值
以内则推进「已改善」。

用法（cwd 不限）：

    python scripts/reconcile_deviations.py                  # 回填当前最新快照
    python scripts/reconcile_deviations.py --warehouse GTJ10036
    python scripts/reconcile_deviations.py --dry-run        # 只打印不回写

只处理 `relocate_job_order_id` 指向的移库单已 `VERIFIED` 的「已发起移库」偏离 —— 与
`_reconcile_relocate_deviation` 同一口径，**不按 `batch_no` 过滤**（`start_relocate` 把
偏离一律当作「按物料收拢散批」的物料级事实，见 `app/services/verify.py`）；「移库单还没
执行完（PENDING/PLANNED）」的偏离不在此推进。不建基线、不进 `ImportSession` 状态机、
不产 `JobOrder`。
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

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.enums import JobStatus  # noqa: E402
from app.models.job import Deviation, DeviationStatus, JobOrder  # noqa: E402
from app.models.linkage import InventoryItem, Snapshot  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="移库完成后的偏离回填（推进已改善）")
    parser.add_argument("--warehouse", default=settings.warehouse_code, help="单厂仓库号")
    parser.add_argument("--dry-run", action="store_true", help="只打印不回写")
    return parser.parse_args(argv)


def _current_cross_aisle(session, *, snapshot_id: int, material_code: str) -> int:
    """同物料跨巷道数 = 该物料在当前快照里出现的巷道数（`location_code[:2]` 去重）。"""
    aisles = session.scalars(
        select(InventoryItem.location_code).where(
            InventoryItem.snapshot_id == snapshot_id,
            InventoryItem.material_code == material_code,
        )
    ).all()
    return len({code[:2] for code in aisles})


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
            print(f"[reconcile_deviations] 仓库 {args.warehouse} 无快照基线，跳过")
            return 0

        # 「已发起移库」且其移库单已 VERIFIED 的偏离 —— 即移库已执行完但状态没推进的存量行。
        rows = session.execute(
            select(Deviation, JobOrder)
            .join(JobOrder, Deviation.relocate_job_order_id == JobOrder.id)
            .where(
                Deviation.warehouse_id == args.warehouse,
                Deviation.status == DeviationStatus.RELOCATE_STARTED,
                JobOrder.status == JobStatus.VERIFIED,
            )
            .order_by(Deviation.id)
        ).all()

        if not rows:
            print("[reconcile_deviations] 无待回填的偏离（已发起移库 + 移库单已执行）")
            return 0

        changed: list[tuple[Deviation, int, int, DeviationStatus, DeviationStatus]] = []
        for deviation, _job in rows:
            before_aisle = deviation.actual_cross_aisle
            before_status = deviation.status
            after = _current_cross_aisle(
                session, snapshot_id=snapshot.id, material_code=deviation.material_code
            )
            new_status = (
                DeviationStatus.IMPROVED
                if after <= deviation.threshold_cross_aisle
                else DeviationStatus.RELOCATE_STARTED
            )
            changed.append((deviation, before_aisle, after, before_status, new_status))
            if not args.dry_run:
                deviation.actual_cross_aisle = after
                deviation.status = new_status

        if not args.dry_run:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    mode = "dry-run" if args.dry_run else "回填完成"
    print(
        f"[reconcile_deviations] 快照 #{snapshot.id}（版本 {snapshot.version_no}）"
        f"{mode}：{len(changed)} 条偏离"
    )
    for deviation, before_aisle, after, before_status, new_status in changed:
        print(
            f"[reconcile_deviations] 偏离 #{deviation.id} 物料 {deviation.material_code} "
            f"跨巷道 {before_aisle} → {after}，"
            f"状态 {before_status.value} → {new_status.value}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
