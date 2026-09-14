"""冷路径网关编排：统一链路「规则算 → 脱敏 → 护栏 → 调用 → 记账 → 双产物」。10 §五/§七。

事实来源：10-AI 辅助能力（冷路径）设计 §五（脱敏）、§七（成本护栏）
          openspec/changes/ai-assist/design.md D4（双产物响应契约）、D10（降级不报错）
          spec `ai-assist`「双产物响应契约」「LLM 侧失败降级不报错」

这是四类能力（①KPI 解读 / ②偏离归因 / ③权重调优 / ④移库方案）共用的**统一后段**。
「规则算」在各自能力里先做（`rule` 是规则侧算好的确定性数据，由调用方传入）；本模块
负责它之后的每一段：

    rule（已算好） → redact（脱敏） → quota（护栏） → client（调用） → quota（记账）
                 → DualProduct（双产物）

降级不报错（D10）：provider 未配置 / 预算耗尽 / 超时 / 不可用 四种情况都返回
`DualProduct`（`rule` 恒有 + `ai_generated=false` + 对应 `degraded_reason`），**绝不抛
异常** —— 抛了就是 5xx，就把「降级」错报成「故障」。

「拒绝」≠「降级」：token 超限与并发饱和是**拒绝**（非 200），不是降级（200 + 规则卡片）。
这两者由 `quota.check_guardrails` 判出后，本模块抛 `ColdPathRejected` 交给端点层映射成
合适的 4xx（「拆分」/「稍后再试」），不混进四种降级路径。

红线 3（LLM 产出不经规则校验不进台账）在这里的体现：LLM 的产出只进 `DualProduct.ai`，
不进任何台账；唯一落库的是 `quota.record_tokens` 的**成本记账**（`AiCostQuota`），与
业务台账（`Ledger`）无关。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm import DegradedReason, client
from app.llm.client import Backend
from app.llm.quota import (
    ConcurrencyGate,
    QuotaVerdict,
    check_guardrails,
    current_tokens,
    estimate_tokens,
    record_tokens,
)
from app.llm.redact import redact


class ColdPathRejected(Exception):
    """冷路径请求被入口护栏**拒绝**（token 超限 / 并发饱和）—— 非降级，端点映射为 4xx。"""

    def __init__(self, verdict: QuotaVerdict) -> None:
        self.verdict = verdict
        super().__init__(f"cold path rejected: {verdict.value}")


@dataclass(frozen=True)
class DualProduct:
    """双产物响应（D4）：`rule` 恒有 + `ai` 可选 + `ai_generated` + `degraded_reason`。

    不变量：`ai_generated == (degraded_reason is None)` —— 有降级原因则无 AI 叙事。
    """

    rule: dict[str, Any]
    ai: str | None
    ai_generated: bool
    degraded_reason: str | None

    def __post_init__(self) -> None:
        if self.ai_generated != (self.degraded_reason is None):
            raise ValueError(
                "双产物不变量被破坏：ai_generated 必须 == (degraded_reason is None)"
            )


def run_cold_path(
    *,
    settings: Settings,
    session: Session,
    warehouse_id: str,
    period: str,
    rule: Mapping[str, Any],
    gate: ConcurrencyGate | None = None,
    backend: Backend | None = None,
    timeout_s: float | None = None,
) -> DualProduct:
    """统一链路后段（脱敏 → 护栏 → 调用 → 记账 → 双产物）。`rule` 须已由规则侧算好。

    `gate` / `backend` / `timeout_s` 仅供测试注入；生产走 `settings` 派生默认值。
    """
    # 1. 脱敏：出境提示词只可能含白名单字段（正向枚举，`redact` 逐字段截留）。
    prompt = json.dumps(redact(rule), ensure_ascii=False, sort_keys=True)

    gate = gate if gate is not None else ConcurrencyGate(settings.llm_max_concurrency)

    # 2. 护栏：三道（预算 / token / 并发）在入口即时拦截。ALLOW 已占并发名额。
    verdict = check_guardrails(
        settings=settings,
        prompt=prompt,
        tokens_consumed=current_tokens(session, warehouse_id=warehouse_id, period=period),
        gate=gate,
    )
    if verdict is QuotaVerdict.BUDGET_EXHAUSTED:
        return DualProduct(
            rule=dict(rule), ai=None, ai_generated=False,
            degraded_reason=DegradedReason.BUDGET_EXHAUSTED.value,
        )
    if verdict is QuotaVerdict.TOKENS_EXCEEDED or verdict is QuotaVerdict.CONCURRENCY_SATURATED:
        raise ColdPathRejected(verdict)

    # 3. 调用 + 4. 记账 + 5. 双产物。ALLOW 分支必须 release 并发名额。
    try:
        completion = client.complete(prompt, settings=settings, timeout_s=timeout_s, backend=backend)
        if completion.ok:
            record_tokens(
                session,
                warehouse_id=warehouse_id,
                period=period,
                tokens=estimate_tokens(prompt),
            )
            return DualProduct(
                rule=dict(rule), ai=completion.text, ai_generated=True, degraded_reason=None
            )
        return DualProduct(
            rule=dict(rule), ai=None, ai_generated=False,
            degraded_reason=completion.degraded_reason.value,  # type: ignore[union-attr]
        )
    finally:
        gate.release()
