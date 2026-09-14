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

from pydantic import BaseModel, ConfigDict, Field, field_serializer

#: AI 标注（spec `ai-assist`「AI 建议标注」）—— 所有 LLM 叙事的强制前缀，让前端只能把
#: `ai` 渲染成琥珀色「AI 建议」，而非系统结论（红线：AI 输出不得渲染成系统结论）。
#: 只挂 `ai`，不挂 `rule`（`rule` 是规则算的确定数据，是合法的系统结论）。
AI_NOTICE = "AI 建议，仅供参考，需人工核实，不自动执行"


def with_ai_notice(ai: str | None) -> str | None:
    """给 LLM 叙事注入 AI 标注。`None`（降级无叙事）不注入 —— 没有叙事就没有标注。"""
    if ai is None:
        return None
    return f"{AI_NOTICE}\n{ai}"


class DualProductResponse(BaseModel):
    """双产物（D4）：`rule` 恒有 + `ai` 可选 + `ai_generated` + `degraded_reason`。

    `ai` 在序列化时**强制注入** AI Notice（`field_serializer`）—— 无论哪个端点返回本
    DTO，LLM 叙事都带着「AI 建议，仅供参考，需人工核实，不自动执行」标注，前端据此
    判「无标注不渲染为系统结论」。`rule` 不加标注（它是规则算的系统结论）。
    """

    rule: dict[str, Any]
    ai: str | None = None
    ai_generated: bool
    degraded_reason: str | None = None

    @field_serializer("ai")
    def _inject_ai_notice(self, ai: str | None) -> str | None:
        return with_ai_notice(ai)


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
