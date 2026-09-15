"""KPI 聚合查询 路由。

事实来源：18-KPI 设计 §1.3（四个看板指标）；19-系统架构与部署视图 附录B。

`GET /api/kpi/summary`：看板四个指标的一次聚合，全部从真实数据计算（`kpi.compute_kpi_summary`）
—— 同物料跨巷道均值从当前快照**库存**取值，拣货量加权集中度 / 单张开单跨巷道均值从出库
台账拣货路径取值，推荐采纳率从入库落位推荐取值。不硬编码演示值。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.kpi import KpiSummary
from app.services import kpi

router = APIRouter(prefix="/api/kpi")


@router.get("/summary", response_model=KpiSummary)
def kpi_summary(
    warehouse_id: str = Query(min_length=1, max_length=32),
    session: Session = Depends(get_db),
) -> KpiSummary:
    """看板四个指标的一次聚合（只读，从真实数据计算）。"""
    return KpiSummary(**kpi.compute_kpi_summary(session, warehouse_id=warehouse_id))
