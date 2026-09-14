"""冷路径端点（`routes/llm.py`）的 DTO。D4 / D13。

事实来源：openspec/changes/ai-assist/design.md D4（双产物响应契约）、D13（端点表）
          spec `ai-assist`「双产物响应契约」「冷路径开关守卫与一键关闭」

双产物响应契约（D4）：`rule`（规则算的确定数据，恒有）+ `ai`（LLM 叙事，可选）+
`ai_generated`（布尔）+ `degraded_reason`（可空）。不变量由网关层
`capabilities.DualProduct.__post_init__` 强制：
`ai_generated == (degraded_reason is None)` —— 本 DTO 是纯载体，不重复校验。

`weight/apply` 是规则侧落地（`services/weight_tune.apply_weight`），无 LLM 叙事，故它
**不是**双产物 —— 有自己的 `WeightApplyResponse`。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DualProductResponse(BaseModel):
    """双产物（D4）：`rule` 恒有 + `ai` 可选 + `ai_generated` + `degraded_reason`。"""

    rule: dict[str, Any]
    ai: str | None = None
    ai_generated: bool
    degraded_reason: str | None = None


class ToggleRequest(BaseModel):
    """开关请求体：`enabled` 开/关。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ToggleResponse(BaseModel):
    """开关响应：回显开关落点后的实际状态。"""

    cold_path_enabled: bool


class KpiInterpretRequest(BaseModel):
    """① KPI 解读。`period` 可选（缺省 = 当前月 `%Y-%m`）。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    period: str | None = None


class DeviationAttributeRequest(BaseModel):
    """② 偏离归因。`material_code` 必填；`batch_no` 可选。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    material_code: str = Field(min_length=1)
    batch_no: str | None = None


class WeightTuneRequest(BaseModel):
    """③ 权重调优建议（影子模式，不生效）。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)


class WeightApplyRequest(BaseModel):
    """③ 采纳落地：定位一条 PROPOSED 建议，规则校验后写新版 `WeightConfig`。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    suggestion_id: int = Field(ge=1)


class WeightApplyResponse(BaseModel):
    """采纳落地的结果：建议号 + 新版本号 + 实际落库的六因子权重。"""

    suggestion_id: int
    version_no: int
    weights: dict[str, float]


class RelocateProposeRequest(BaseModel):
    """④ 移库方案生成（只读，试算不落库）。`material_code` 必填。"""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: str = Field(min_length=1, max_length=32)
    material_code: str = Field(min_length=1)
