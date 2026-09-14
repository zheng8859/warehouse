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
from datetime import datetime, time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.config_version import pick_current_version
from app.core.enums import JobType
from app.engine.factors import load_snapshot_index
from app.engine.reserved import available_cap
from app.engine.scoring import load_weights
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
from app.models.configuration import Capability, CapacityConfig
from app.models.job import Deviation, DeviationCauseKind, JobOrder, RecommendationPlan
from app.models.llm import AiSuggestion, AiSuggestionStatus
from app.models.linkage import AisleCap, InventoryItem, Snapshot
from app.models.master_data import Material
from app.services.kpi import same_material_cross_aisle_mean
from app.services.relocate import build_relocate_plans
from app.services.weight_tune import (
    MIN_WEIGHT_TUNE_SAMPLES,
    build_counterfactual,
    load_samples,
)


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


# ---------------------------------------------------------------------------
# ② 偏离归因（只读，P1）—— 规则算异常清单 → LLM 归纳叙事，不断言唯一根因。
# ---------------------------------------------------------------------------


def build_deviation_rule(
    *,
    material_code: str,
    batch_no: str | None = None,
    actual_cross_aisle: int | None = None,
    threshold_cross_aisle: int | None = None,
    recommended_aisles: list[str] | None = None,
    factor_scores: Mapping[str, float] | None = None,
    factor_degraded: Mapping[str, str] | None = None,
    plan_degraded: bool = False,
    plan_degrade_reason: str | None = None,
    cap_note: str | None = None,
    actual_location_code: str | None = None,
    cause_kind: str | None = None,
) -> dict[str, Any]:
    """② 规则卡片：四类候选（容量/降级/人工/非系统）+ 多源明细，全由规则侧确定。

    四类候选的「事实」各从规则侧数据来（D11「异常清单规则算」）：

    - **容量**：方案级降级（`degraded` = 容量不足走降级链，`17` §10.1）+ 分配时点的
      cap 事实（`cap_note`，来自推荐理由 `breakdown` 的 cap 因子注记「可用 X/Y 板」）。
    - **降级**：因子级降级（`factor_degraded` 逐条「因子 → 原因」）。
    - **人工**：实际落位不在推荐巷道集内（操作员微调 / 绕开推荐）。
    - **非系统**：偏离成因（`cause_kind`：历史库存拖累 / 新入库收拢不达标）。

    本函数**只列事实、不下结论**：每个候选的 `evidence` 是事实清单，非空只说明「这条
    线索有据可查」，系统不断言哪一条是唯一根因 —— 归纳措辞是 LLM 的事，事实不许 LLM
    新增（红线「LLM 只叙事、不算数」在 ② 的落点）。确定性：同输入必得同清单。
    """
    aisles = sorted(recommended_aisles or ())

    capacity_evidence: list[str] = []
    if plan_degraded and plan_degrade_reason:
        capacity_evidence.append(f"方案级降级（容量不足）：{plan_degrade_reason}")
    if cap_note is not None:
        capacity_evidence.append(f"分配时点 cap 事实：{cap_note}")

    degradation_evidence: list[str] = [
        f"{factor} 因子降级：{reason}"
        for factor, reason in sorted((factor_degraded or {}).items())
    ]

    manual_evidence: list[str] = []
    if (
        actual_location_code is not None
        and aisles
        and actual_location_code[:2] not in aisles
    ):
        manual_evidence.append(
            f"实际落位 {actual_location_code} 不在推荐巷道 {aisles} 内（人工微调 / 绕开推荐）"
        )

    nonsystem_evidence: list[str] = []
    if cause_kind == DeviationCauseKind.LEGACY_INVENTORY_DRAG.value:
        nonsystem_evidence.append(
            "成因分类：历史库存拖累（散射为存量分布，非本次系统分配造成）"
        )
    elif cause_kind == DeviationCauseKind.NEW_INBOUND_SHORTFALL.value:
        nonsystem_evidence.append("成因分类：新入库收拢不达标（本次落位造成）")

    candidates = [
        {"category": "容量", "evidence": capacity_evidence},
        {"category": "降级", "evidence": degradation_evidence},
        {"category": "人工", "evidence": manual_evidence},
        {"category": "非系统", "evidence": nonsystem_evidence},
    ]

    metrics: dict[str, Any] = {
        "actual_cross_aisle": actual_cross_aisle,
        "threshold_cross_aisle": threshold_cross_aisle,
        "recommended_aisles": aisles,
        "factor_scores": dict(factor_scores or {}),
        "actual_location_code": actual_location_code,
        "candidates": candidates,
    }
    rule: dict[str, Any] = {"material_code": material_code, "metrics": metrics}
    if batch_no is not None:
        rule["batch_no"] = batch_no
    return rule


def deviation_attribute(
    session: Session,
    *,
    warehouse_id: str,
    material_code: str,
    batch_no: str | None = None,
    settings: Settings | None = None,
    period: str | None = None,
    gate: ConcurrencyGate | None = None,
    backend: Backend | None = None,
    timeout_s: float | None = None,
) -> DualProduct:
    """② 偏离归因：规则侧拉多源明细 → 四类候选 → 脱敏 → 网关 → 双产物。只读。

    多源明细 = 偏离批次（`Deviation`）的实测/阈值 + 最近一张入库作业单的推荐巷道集、
    6 因子分值、实际落位，与推荐理由里的分配时点 cap 事实。全部是规则侧确定性取数，
    LLM 只归纳。无偏离 / 无方案时规则卡片仍产出（空证据），不报错。
    """
    settings = settings if settings is not None else Settings()
    period = period if period is not None else datetime.now().strftime("%Y-%m")

    # 1. 最近一条偏离（物料 + 可选批号）。
    dev_stmt = sa.select(Deviation).where(
        Deviation.warehouse_id == warehouse_id,
        Deviation.material_code == material_code,
    )
    if batch_no is not None:
        dev_stmt = dev_stmt.where(Deviation.batch_no == batch_no)
    deviation = session.scalars(
        dev_stmt.order_by(Deviation.created_at.desc(), Deviation.id.desc()).limit(1)
    ).first()

    # 2. 最近一张入库作业单（该物料 + 可选批号）与其最新推荐方案。
    job_stmt = sa.select(JobOrder).where(
        JobOrder.warehouse_id == warehouse_id,
        JobOrder.job_type == JobType.INBOUND,
        JobOrder.material_code == material_code,
    )
    if batch_no is not None:
        job_stmt = job_stmt.where(JobOrder.batch_no == batch_no)
    job = session.scalars(job_stmt.order_by(JobOrder.id.desc()).limit(1)).first()

    plan = None
    if job is not None:
        plan = session.scalars(
            sa.select(RecommendationPlan)
            .where(RecommendationPlan.job_order_id == job.id)
            .order_by(RecommendationPlan.id.desc())
            .limit(1)
        ).first()

    payload = plan.payload_json if plan is not None else None
    recommended_aisles = list(payload.get("aisles", [])) if payload else []
    factor_scores = payload.get("factors") if payload else None
    factor_degraded = payload.get("factor_degraded") if payload else None
    plan_degraded = bool(payload.get("degraded", False)) if payload else False
    plan_degrade_reason = payload.get("degrade_reason") if payload else None

    # 分配时点的 cap 事实：推荐巷道集首位（主巷道）的 cap 因子注记。
    cap_note: str | None = None
    if payload and recommended_aisles:
        breakdown = payload.get("breakdown", {})
        cap_term = breakdown.get(recommended_aisles[0], {}).get("cap")
        if isinstance(cap_term, Mapping):
            cap_note = cap_term.get("note")

    rule = build_deviation_rule(
        material_code=material_code,
        batch_no=batch_no,
        actual_cross_aisle=deviation.actual_cross_aisle if deviation else None,
        threshold_cross_aisle=deviation.threshold_cross_aisle if deviation else None,
        recommended_aisles=recommended_aisles,
        factor_scores=factor_scores,
        factor_degraded=factor_degraded,
        plan_degraded=plan_degraded,
        plan_degrade_reason=plan_degrade_reason,
        cap_note=cap_note,
        actual_location_code=job.actual_location_code if job is not None else None,
        cause_kind=deviation.cause_kind.value if deviation else None,
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


# ---------------------------------------------------------------------------
# ③ 权重调优（离线，人采纳才生效）—— 反事实模拟规则算 → LLM 叙事，影子模式落建议。
# ---------------------------------------------------------------------------


def weight_tune(
    session: Session,
    *,
    warehouse_id: str,
    settings: Settings | None = None,
    period: str | None = None,
    now: datetime | None = None,
    gate: ConcurrencyGate | None = None,
    backend: Backend | None = None,
    timeout_s: float | None = None,
) -> DualProduct:
    """③ 权重调优建议：历史批次反事实模拟（规则算）→ 脱敏 → 网关 → 双产物。**影子模式**。

    门槛（D12）：历史批次样本 < 50 → `insufficient_samples` 降级，**不调 LLM、不落建议**
    —— 样本太少时反事实模拟的统计没有意义，与其让 LLM 对着噪声编一套「调参理由」，
    不如诚实降级。

    ≥50 时：反事实表（`build_counterfactual`）算出「提议权重」，先落一张
    `AiSuggestion(status=PROPOSED)`（影子模式 = 不生效），再走统一链路拿 LLM 叙事。
    建议的 `context_json` 存的是**规则算的拟采纳权重**，不是 LLM 文本 —— `weight/apply`
    正是读它才写 `WeightConfig`（红线 3：采纳规则算的数，LLM 只叙事）。

    `rule` 卡片里补一枚 `suggestion_id` 让 `weight/apply` 定位建议；它不是白名单字段，
    脱敏时会被剥掉，不进出站提示词。
    """
    settings = settings if settings is not None else Settings()
    period = period if period is not None else datetime.now().strftime("%Y-%m")
    now = now if now is not None else datetime.now()

    samples = load_samples(session, warehouse_id=warehouse_id)
    if len(samples) < MIN_WEIGHT_TUNE_SAMPLES:
        return DualProduct(
            rule={
                "metrics": {
                    "sample_size": len(samples),
                    "required_sample_size": MIN_WEIGHT_TUNE_SAMPLES,
                }
            },
            ai=None,
            ai_generated=False,
            degraded_reason=DegradedReason.INSUFFICIENT_SAMPLES.value,
        )

    current_weights = load_weights(session, warehouse_id=warehouse_id, now=now)
    counterfactual = build_counterfactual(
        samples=samples, current_weights=current_weights
    )
    rule: dict[str, Any] = {"metrics": counterfactual}

    # 影子模式：建议先落 PROPOSED（不生效）。context_json = 规则算的拟采纳权重。
    suggestion = AiSuggestion(
        warehouse_id=warehouse_id,
        capability_kind=Capability.WEIGHT_TUNING,
        suggestion_text="",  # LLM 叙事在 run_cold_path 之后回填。
        status=AiSuggestionStatus.PROPOSED,
        context_json={
            "proposed_weights": counterfactual["proposed_weights"],
            "current_weights": counterfactual["current_weights"],
            "sample_size": len(samples),
        },
    )
    session.add(suggestion)
    session.flush()
    rule["suggestion_id"] = suggestion.id

    result = run_cold_path(
        settings=settings,
        session=session,
        warehouse_id=warehouse_id,
        period=period,
        rule=rule,
        gate=gate,
        backend=backend,
        timeout_s=timeout_s,
    )
    # 回填 LLM 叙事（降级时保持空串，建议本身仍可被人工采纳 —— 反事实表是规则算的）。
    suggestion.suggestion_text = result.ai or ""

    return DualProduct(
        rule=rule,
        ai=result.ai,
        ai_generated=result.ai_generated,
        degraded_reason=result.degraded_reason,
    )


# ---------------------------------------------------------------------------
# ④ 移库方案（只读，P1）—— 规则算多方案 + 量化代价 + 三重校验 → LLM 只叙事。
# ---------------------------------------------------------------------------


def relocate_propose(
    session: Session,
    *,
    warehouse_id: str,
    material_code: str,
    settings: Settings | None = None,
    period: str | None = None,
    now: datetime | None = None,
    gate: ConcurrencyGate | None = None,
    backend: Backend | None = None,
    timeout_s: float | None = None,
) -> DualProduct:
    """④ 移库方案：规则算多方案（激进/均衡/保守）+ 量化代价 + 三重校验 → LLM 只叙事。**只读**。

    规则侧 = `build_relocate_plans`（纯函数，D8「多方案规则算」）：读最新快照的物料级
    板数分布 + 各巷可用格数，产出三档收拢**试算**（不落库、不写 `JobOrder`）。LLM 只做
    「权衡利弊」叙事，数字永远来自规则（红线「规则算、LLM 只叙事」）。

    落地走 `relocate.operate` 二次确认（D8 复用 G3），本函数**不写台账** —— 未经确认
    不产生 `JobOrder`（红线 2「LLM 不直接执行写操作」）。可用格数口径随物料 ABC
    （`available_cap`，与入库分配同源），阈值与释放钟点读 `CapacityConfig`、缺席退回
    `Settings` 引导值（与 `allocate._load_capacity_settings` 同口径）。
    """
    settings = settings if settings is not None else Settings()
    period = period if period is not None else datetime.now().strftime("%Y-%m")
    now = now if now is not None else datetime.now()

    sid = _latest_snapshot_id(session, warehouse_id=warehouse_id)
    plates_by_aisle: dict[str, int] = {}
    if sid is not None:
        plates_by_aisle = dict(
            load_snapshot_index(session, snapshot_id=sid)
            .profile(material_code)
            .plates_by_aisle
        )

    abc_class = session.scalar(
        sa.select(Material.abc_class).where(
            Material.warehouse_id == warehouse_id,
            Material.material_code == material_code,
        )
    )
    config_row = pick_current_version(
        session.scalars(
            sa.select(CapacityConfig).where(CapacityConfig.warehouse_id == warehouse_id)
        ),
        now=now,
    )
    release_at = (
        config_row.reserved_release_at
        if config_row is not None
        else time.fromisoformat(settings.reserve_release_at)
    )
    threshold = (
        config_row.same_material_cross_aisle_threshold
        if config_row is not None
        else settings.same_material_cross_aisle_max
    )

    available: dict[str, int] = {}
    if sid is not None:
        cap_rows = {
            cap.aisle_no: cap
            for cap in session.scalars(
                sa.select(AisleCap).where(
                    AisleCap.warehouse_id == warehouse_id,
                    AisleCap.snapshot_id == sid,
                )
            )
        }
        available = {
            aisle: (
                0
                if (cap := cap_rows.get(aisle)) is None
                else available_cap(
                    cap=cap, abc_class=abc_class, release_at=release_at, now=now
                )
            )
            for aisle in plates_by_aisle
        }

    plans = build_relocate_plans(
        plates_by_aisle=plates_by_aisle,
        available=available,
        cross_aisle_threshold=threshold,
    )
    rule: dict[str, Any] = {"material_code": material_code, "metrics": plans}

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
