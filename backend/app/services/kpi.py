"""KPI 聚合：偏离清单（移库任务来源）→ 后续 `KpiSnapshot` 聚合（18 附录A）。

事实来源：15-入库出库移库与后验流程设计 §7.2（偏离批次治理回路）
          18-KPI 设计（加权集中度 / 采纳率 / 落位准确率 → KpiSnapshot）
          openspec/changes/transaction-base/design.md D5（本 change 只提供偏离清单聚合）
          spec `transaction-base`「偏离批次标记」

阶段四只落「偏离清单」这一个读入口：后验超标写入的 `Deviation` 行
（`verify._write_deviations`）是移库作业的任务来源（`Verification → Deviation → JobOrder`，
17 §4.4）。`KpiSnapshot` 的加权集中度 / 采纳率 / 落位准确率聚合属阶段六，本文件不实现。
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.job import Deviation, DeviationStatus


def list_deviations(
    session: Session,
    *,
    warehouse_id: str,
    status: DeviationStatus | None = None,
) -> list[Deviation]:
    """按仓列出偏离清单，`status` 可选过滤（默认全部），按 `created_at` 升序返回。

    「偏离清单」是移库作业的任务来源（15 §7.2）：操作员从这里挑偏离批次发起收拢。
    不做分页 / 聚合 —— 阶段六的 KPI 看板再补，本函数只落「可查」（design.md D5）。
    """
    stmt = sa.select(Deviation).where(Deviation.warehouse_id == warehouse_id)
    if status is not None:
        stmt = stmt.where(Deviation.status == status)
    stmt = stmt.order_by(Deviation.created_at, Deviation.id)
    return list(session.scalars(stmt))
