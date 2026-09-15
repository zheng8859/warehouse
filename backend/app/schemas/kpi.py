"""KPI 看板聚合响应体。

事实来源：18-KPI 设计 §1.3（四个看板指标）。
"""

from __future__ import annotations

from pydantic import BaseModel


class KpiSummary(BaseModel):
    """`GET /api/kpi/summary` 的响应体 —— 四个看板指标，全部从真实数据聚合。"""

    material_cross_aisle_mean: float
    weighted_concentration: float
    per_do_cross_aisle_mean: float
    adoption_rate: float
