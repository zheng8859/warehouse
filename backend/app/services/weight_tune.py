"""③ 权重调优（离线，人采纳才生效）的规则侧：样本计数 + 反事实模拟。10 §五。

事实来源：10-AI 辅助能力（冷路径）设计 §五（③ 统计各因子 + 反事实模拟 + 人采纳才生效）
          openspec/changes/ai-assist/design.md D12（>=50 批次门槛，反事实规则算，影子模式）
          spec `ai-assist`「③ 权重调优（≥50 批次，人采纳才生效）」

`load_samples`：历史批次样本 = 该仓入库分配方案（`RecommendationPlan.plan_kind=ASSIGN`）行。
每个样本是一份「推荐理由」（`ReasonPayload`：`breakdown` 每巷每因子取值 + `aisles` 选中巷道），
是反事实模拟的输入。`RecommendationPlan` 挂 `warehouse_id`（`BaseEntity` 公共列），直接过滤即可。

`build_counterfactual`：反事实模拟（纯函数、确定性）—— 调高某因子权重，历史批次推荐会怎么变。
输出「建议表」：每因子当前/提议权重 + 敏感性（选中巷道取值 − 候选均值），及提议权重下的
预期影响（选中巷道得分均值变化）。提议权重 = 敏感性最高因子 +step、最低因子 −step（一加一减
权重和不变）。LLM 只把它叙述成「建议表 + 依据」，数字不改写（红线「规则算、LLM 只叙事」）。

影子模式 = 建议落在 `AiSuggestion.status=PROPOSED`（不生效）；仅在 `ai.weight.update` 采纳后
才写 `WeightConfig`（见 `capabilities.apply_weight`）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.configuration import WEIGHT_FACTORS
from app.models.job import PlanKind, RecommendationPlan

#: ③ 的样本门槛（D12：2026-09-14 拍板覆盖 doc 10 §五/§八 的 >=300）。
MIN_WEIGHT_TUNE_SAMPLES = 50

#: 反事实模拟的单步调权幅度。敏感性最高/最低因子各 ±step，一加一减权重和不变。
COUNTERFACTUAL_STEP = 0.05


def load_samples(session: Session, *, warehouse_id: str) -> list[dict[str, Any]]:
    """历史批次推荐理由的 payload 列表（反事实模拟的输入）。

    只取 `ASSIGN`：6 因子评分只发生在入库分配，顺路取 / 收拢方案的 payload 无 `breakdown`，
    进不了反事实模拟。`warehouse_id` 是 `BaseEntity` 公共列，直接过滤（17 §十一 数据隔离）。
    """
    return list(
        session.scalars(
            sa.select(RecommendationPlan.payload_json).where(
                RecommendationPlan.warehouse_id == warehouse_id,
                RecommendationPlan.plan_kind == PlanKind.ASSIGN,
            )
        )
    )


def build_counterfactual(
    *,
    samples: Sequence[Mapping[str, Any]],
    current_weights: Mapping[str, float],
    step: float = COUNTERFACTUAL_STEP,
) -> dict[str, Any]:
    """反事实模拟（纯函数、确定性）：调高某因子权重，历史批次推荐会怎么变。

    每因子的**敏感性** = 选中巷道该因子取值 − 该因子在候选巷道的均值（对全部样本取平均）。
    正值说明该因子「拉向」了历史选中巷道（调高会强化现状），负值说明它「反向」于选中巷道。

    **提议权重** = 敏感性最高因子 +step、最低因子 −step（夹取 [0,1]），其余不动 —— 一加一减
    故权重和不变。**预期影响** = 选中巷道得分均值（当前权重 vs 提议权重），用纯加权和重放。

    输出是「建议表」的数字来源，LLM 只叙述、不改写（红线「规则算、LLM 只叙事」）。
    """
    sensitivity: dict[str, float] = {}
    for factor in WEIGHT_FACTORS:
        diffs: list[float] = []
        for sample in samples:
            breakdown = sample.get("breakdown") or {}
            chosen = (sample.get("aisles") or [None])[0]
            if not chosen or chosen not in breakdown or factor not in breakdown[chosen]:
                continue
            values = [
                terms[factor]["value"]
                for terms in breakdown.values()
                if factor in terms and "value" in terms[factor]
            ]
            if not values:
                continue
            diffs.append(breakdown[chosen][factor]["value"] - (sum(values) / len(values)))
        sensitivity[factor] = (sum(diffs) / len(diffs)) if diffs else 0.0

    ranked = sorted(WEIGHT_FACTORS, key=lambda f: sensitivity[f])
    top, bottom = ranked[-1], ranked[0]
    proposed = {f: float(current_weights[f]) for f in WEIGHT_FACTORS}
    proposed[top] = min(1.0, proposed[top] + step)
    proposed[bottom] = max(0.0, proposed[bottom] - step)

    def chosen_score_mean(weights: Mapping[str, float]) -> float:
        totals: list[float] = []
        for sample in samples:
            breakdown = sample.get("breakdown") or {}
            chosen = (sample.get("aisles") or [None])[0]
            degraded = sample.get("factor_degraded") or {}
            scored = [f for f in WEIGHT_FACTORS if f not in degraded]
            denominator = sum(weights[f] for f in scored)
            if not chosen or chosen not in breakdown or denominator == 0:
                continue
            if set(breakdown[chosen]) != set(scored):
                continue
            numerator = sum(weights[f] * breakdown[chosen][f]["value"] for f in scored)
            totals.append(numerator / denominator)
        return (sum(totals) / len(totals)) if totals else 0.0

    return {
        "sample_size": len(samples),
        "current_weights": {f: float(current_weights[f]) for f in WEIGHT_FACTORS},
        "proposed_weights": {f: proposed[f] for f in WEIGHT_FACTORS},
        "sensitivity": {f: round(sensitivity[f], 4) for f in WEIGHT_FACTORS},
        "expected_impact": {
            "chosen_score_mean_current": round(chosen_score_mean(current_weights), 4),
            "chosen_score_mean_proposed": round(chosen_score_mean(proposed), 4),
        },
        "factors": [
            {
                "factor": f,
                "current_weight": float(current_weights[f]),
                "proposed_weight": proposed[f],
                "sensitivity": round(sensitivity[f], 4),
            }
            for f in WEIGHT_FACTORS
        ],
    }
