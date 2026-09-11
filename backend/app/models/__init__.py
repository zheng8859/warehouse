"""SQLAlchemy 模型 —— 23 个实体，按 17 号四条数据链分组。

事实来源：17-数据模型设计（实体共 23 个，枚举 11 个）

  主数据链  仓库 → 巷道 → 库位                                 master_data.py      (6)
  衔接链    ImportSession → Snapshot → InventoryItem/AisleCap  linkage.py          (5)
  作业链    JobOrder → RecommendationPlan → Ledger → Verification  job.py          (5)
  KPI       KpiSnapshot                                        kpi.py              (1)
  身份链    Account → 角色 → 权限                               identity.py         (1)
  配置      WeightConfig / CapacityConfig / FieldMappingConfig / PromptTemplate
            / ConversationContext                              configuration.py    (5)

  合计 6 + 5 + 5 + 1 + 1 + 5 = 23

注意：`操作与确认记录`（17 §4.5）不是 23 实体之一 —— 它没有英文实体名、不在 ER 图中，
处置结果以 disposition 枚举落在 JobOrder 上。不要为它新建模型。

导入顺序敏感：本模块必须导入全部模型，确保 Base.metadata 完整（建表与迁移依赖）。
"""
from __future__ import annotations

# 已实现的实体逐个登记；尚未实现的模块只有 docstring，导入它们是空操作，
# 但先挂上可以保证后续往模块里加实体时自动进 Base.metadata。
from app.models import (  # noqa: F401
    configuration,
    identity,
    job,
    kpi,
    linkage,
    master_data,
)
from app.models.base import Base  # noqa: F401

__all__ = [
    "Base",
    "configuration",
    "identity",
    "job",
    "kpi",
    "linkage",
    "master_data",
]
