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
from datetime import datetime
from typing import Any

import sqlalchemy as sa
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
from app.models.linkage import InventoryItem, Snapshot
from app.services.kpi import same_material_cross_aisle_mean


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


# ---------------------------------------------------------------------------
# ① KPI 解读（只读，P1）—— 规则聚合 → LLM 叙事，数值不改写。
# ---------------------------------------------------------------------------


def build_kpi_rule(
    *,
    cross_aisle_mean: float,
    weighted_concentration: int,
    adoption_rate: float,
    material_code: str | None = None,
) -> dict[str, Any]:
    """① 规则卡片：聚合指标挂在白名单键 `metrics` 下。

    `metrics` 是 `redact` 的正向白名单键，出境不被剥掉；其内部三个数值是规则侧算的
    数，LLM 只叙事、不得改写。这样「出站提示词里的数值」与「rule 卡片的数值」同源，
    spec ① 的「数值一致」由结构保证。
    """
    metrics: dict[str, Any] = {
        "same_material_cross_aisle_mean": cross_aisle_mean,
        "weighted_concentration": weighted_concentration,
        "adoption_rate": adoption_rate,
    }
    rule: dict[str, Any] = {"metrics": metrics}
    if material_code is not None:
        rule["material_code"] = material_code
    return rule


def _latest_snapshot_id(session: Session, *, warehouse_id: str) -> int | None:
    """当前（最新版本号）快照 id；无快照 → None。"""
    return session.scalars(
        sa.select(Snapshot.id)
        .where(Snapshot.warehouse_id == warehouse_id)
        .order_by(Snapshot.version_no.desc(), Snapshot.id.desc())
        .limit(1)
    ).one_or_none()


def kpi_interpret(
    session: Session,
    *,
    warehouse_id: str,
    settings: Settings | None = None,
    snapshot_id: int | None = None,
    period: str | None = None,
    gate: ConcurrencyGate | None = None,
    backend: Backend | None = None,
    timeout_s: float | None = None,
) -> DualProduct:
    """① KPI 解读：规则聚合（同物料跨巷道均值）→ 脱敏 → 网关 → 双产物。

    `weighted_concentration` / `adoption_rate` 的聚合属**阶段六收编点**（KpiSnapshot
    全量聚合落地时补齐 DO 加权集中度与推荐日志采纳率），本阶段最小口径给 0 —— 结构
    稳定、只等阶段六填数。数值一旦算出来就与 `rule` 同源，LLM 只叙事。
    """
    settings = settings if settings is not None else Settings()
    period = period if period is not None else datetime.now().strftime("%Y-%m")

    sid = snapshot_id if snapshot_id is not None else _latest_snapshot_id(
        session, warehouse_id=warehouse_id
    )
    cross_aisle_mean = 0.0
    if sid is not None:
        rows = session.execute(
            sa.select(InventoryItem.material_code, InventoryItem.location_code).where(
                InventoryItem.snapshot_id == sid
            )
        )
        cross_aisle_mean = same_material_cross_aisle_mean(
            (material, location[:2]) for material, location in rows
        )

    rule = build_kpi_rule(
        cross_aisle_mean=cross_aisle_mean,
        weighted_concentration=0,
        adoption_rate=0.0,
    )
    return run_cold_path(
        settings=settings,
        session=session,
        warehouse_id=warehouse_id,
        period=period,
        rule=rule,
        gate=gate,
        backend=backend,
        timeout_s=timeout_s,
    )
