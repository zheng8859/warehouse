"""快照查询 路由（`GET /api/snapshot/current`）。

事实来源：19-系统架构与部署视图 附录B；16-数据衔接与 cap 自维护 附录B
          spec `data-import`「数据导入页数据层」（时点卡 / 快照版本展示）
          openspec/changes/data-import/design.md D7（p2 接 `/api/snapshot/current`）
          tasks.md 6.1

`GET /api/snapshot/current` 返回最新快照（版本号最大）的展示形状：
`{snapshot_id, version_no, snapshot_time, snapshot_version}`。`snapshot_version` 是
`snapshot_time` 的 `"%Y-%m-%dT%H:%M"` 渲染 —— 与 `cap/baseline.py::_render_snapshot_version`
及 `reason.py` 同一格式（那是报文层形状，不是 `version_no` 那个整数）。无快照 → 404，
不猜测、不兜底（CLAUDE.md §四「快照缺失阻断」）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.errors import NotFound
from app.models.linkage import Snapshot

router = APIRouter(prefix="/api/snapshot")


@router.get("/current")
def current_snapshot(db: Session = Depends(get_db)) -> dict:
    """最新快照（版本号最大，按仓库单调递增）。无快照 → 404。"""
    snapshot = db.scalar(
        select(Snapshot)
        .where(Snapshot.warehouse_id == settings.warehouse_code)
        .order_by(Snapshot.version_no.desc())
        .limit(1)
    )
    if snapshot is None:
        raise NotFound("尚无快照基线，请先执行数据导入")
    return {
        "snapshot_id": snapshot.id,
        "version_no": snapshot.version_no,
        "snapshot_time": snapshot.snapshot_time.isoformat(),
        "snapshot_version": snapshot.snapshot_time.strftime("%Y-%m-%dT%H:%M"),
    }
