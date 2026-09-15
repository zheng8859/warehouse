"""冷路径 AI 辅助（阶段五 / 设计 10）—— 可选增强，默认打开、一键关闭。

红线：LLM 不参与实时评分与排序；不直接执行写操作；产出不经规则校验不进台账。
一键关闭后核心链路不受任何影响。

核心链路（`app/engine/`、`app/services/`、`app/cap/`、`app/importer/`）**不得 import
本包**；本包只被 `app/api/routes/` 的冷路径端点与 `app/schemas/` 的冷路径 DTO 引用。
"""
from __future__ import annotations

from enum import Enum


class DegradedReason(str, Enum):
    """冷路径「降级不报错」的降级原因（spec `ai-assist`「LLM 侧失败降级不报错」+ ③ 样本门槛）。

    这是响应契约的一部分（DTO 的 `degraded_reason` 字段取值），不是内部实现细节。
    不变量：`ai_generated == (degraded_reason is None)` —— 有降级原因则无 AI 叙事。
    """

    PROVIDER_UNCONFIGURED = "provider_unconfigured"  # 未配置 provider，不发起调用
    BUDGET_EXHAUSTED = "budget_exhausted"            # 月度预算耗尽，入口熔断
    LLM_TIMEOUT = "llm_timeout"                      # 外部调用超时
    LLM_UNAVAILABLE = "llm_unavailable"              # 外部服务连接失败 / 不可用
    INSUFFICIENT_SAMPLES = "insufficient_samples"    # ③ 权重调优：历史批次 < 50
    INTENT_UNRECOGNIZED = "intent_unrecognized"      # L2 NLU 未产出合法意图（解析失败 / 无意图）
