"""巷道容量自维护 路由（`GET /api/cap`、`POST /api/cap/recompute`）。

事实来源：19-系统架构与部署视图 附录B；16-数据衔接与 cap 自维护 附录B（§6.4 漂移校正）
          spec `data-import`「cap 基线全量重算」（快照导入触发全量重算与新版本归档）
          openspec/changes/data-import/design.md D5（cap 四项口径）
          tasks.md 6.1

`GET /api/cap` 返回**当前快照**的巷道 cap 列表（可选 `?aisle=` 过滤），按 `aisle_no`
升序。`POST /api/cap/recompute` 对当前快照就地重算（漂移校正，16 §6.4「以快照重算值为
准」）—— 快照是权威，重算只把派生值拉回权威口径，不产生新版本、不迁移会话状态。

读侧只读快照（`AisleCap` 是快照产物），**不落台账**：台账是 cap 增量的唯一来源
（16 §6.3），那是 `increment.py` 的事，不在本路由。无快照 → 404（不猜测落位）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.cap.baseline import recompute_snapshot_caps
from app.core.config import settings
from app.core.errors import NotFound
from app.models.linkage import AisleCap, Snapshot

router = APIRouter(prefix="/api/cap")


def _current_snapshot(db: Session) -> Snapshot:
    """当前快照（版本号最大）。无快照 → 404。"""
    snapshot = db.scalar(
        select(Snapshot)
        .where(Snapshot.warehouse_id == settings.warehouse_code)
        .order_by(Snapshot.version_no.desc())
        .limit(1)
    )
    if snapshot is None:
        raise NotFound("尚无快照基线，请先执行数据导入")
    return snapshot


def _cap_dict(cap: AisleCap) -> dict:
    """巷道 cap 行的报文形状（四项口径 + 近站台三态）。"""
    return {
        "aisle_no": cap.aisle_no,
        "cap_physical": cap.cap_physical,
        "cap_total": cap.cap_total,
        "cap_reserved": cap.cap_reserved,
        "cap_usable": cap.cap_usable,
        "is_near_station": cap.is_near_station,
    }


@router.get("")
def list_caps(
    aisle: str | None = Query(default=None, min_length=2, max_length=2),
    db: Session = Depends(get_db),
) -> list[dict]:
    """当前快照的巷道 cap 列表（可选 `?aisle=` 过滤），按 `aisle_no` 升序。"""
    snapshot = _current_snapshot(db)
    stmt = select(AisleCap).where(AisleCap.snapshot_id == snapshot.id)
    if aisle is not None:
        stmt = stmt.where(AisleCap.aisle_no == aisle)
    stmt = stmt.order_by(AisleCap.aisle_no)
    return [_cap_dict(cap) for cap in db.scalars(stmt)]


@router.post("/recompute")
def recompute(db: Session = Depends(get_db)) -> dict:
    """对当前快照就地重算 cap（漂移校正，16 §6.4）。"""
    snapshot = _current_snapshot(db)
    caps = recompute_snapshot_caps(db, snapshot=snapshot)
    db.commit()
    return {
        "snapshot_id": snapshot.id,
        "version_no": snapshot.version_no,
        "aisles": [_cap_dict(cap) for cap in caps],
    }
