"""评测口径门面：L1/L3 测试经这里走业务函数，保证「评测口径 = 业务口径」同源。

事实来源：30-Evals评测体系 §6.5（5 个纯函数）
          openspec/changes/evals/design.md D4（收编既有函数，不重复实现）

本文件是**门面，不重复实现业务逻辑**。每个函数要么 re-export 既有模块的纯函数，
要么做一层「只改入参/出参形态、不碰口径」的薄包装；口径若变，只改业务源一处，
评测自动跟随（同源不漂移）。L1/L3 测试一律 `from evals.eval_utils import ...`。

| 30号 §6.5 函数 | 收编自 |
|---|---|
| `weighted_concentration` | `app/services/kpi.weighted_concentration`（re-export） |
| `cross_aisle` | `app/services/kpi.same_material_cross_aisle_mean`（另加逐物料计数薄包装） |
| `normalize_weights` | `app/engine/scoring.score_aisles` 的分母归一化口径（薄包装） |
| `select_degrade` | `app/engine/degradation.resolve_tiers`（re-export） |
| `assert_no_pii` | `app/llm/redact.redact`（包装成「脱敏后不得残留敏感字段」断言） |
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.engine.degradation import TierPlan, resolve_tiers
from app.llm.redact import FORBIDDEN_OUTBOUND_FIELDS, redact
from app.services.kpi import (
    adoption_rate,  # noqa: F401  再导出供 L3 采纳率阈值判定用
    same_material_cross_aisle_mean,
    weighted_concentration,  # noqa: F401  re-export（见 docstring 表）
)

__all__ = [
    "weighted_concentration",
    "cross_aisle",
    "cross_aisle_mean",
    "normalize_weights",
    "select_degrade",
    "assert_no_pii",
    "attainment_rate",
    "judge_attainment",
    "trend",
]


def cross_aisle(items: Iterable[tuple[str, str]]) -> dict[str, int]:
    """逐物料跨巷道计数：`{material_code: 占用巷道数}`（18 §1.3 L39 的分子侧）。

    与 `same_material_cross_aisle_mean` 同口径（`aisle_no = location_code[:2]`），
    只是把「取均值」之前那个 `dict[str, set[str]]` 摊开成计数返回，供 L3 逐条
    断言「同物料跨巷道 ≤5」时用。同一料号多行只数一次（去重）。
    """
    aisles: dict[str, set[str]] = {}
    for material, aisle in items:
        aisles.setdefault(material, set()).add(aisle)
    return {material: len(a) for material, a in aisles.items()}


def cross_aisle_mean(items: Iterable[tuple[str, str]]) -> float:
    """同物料跨巷道均值（re-export `same_material_cross_aisle_mean`），目标 ≤5。"""
    return same_material_cross_aisle_mean(items)


def normalize_weights(
    weights: Mapping[str, float], *, degraded: Collection[str] = ()
) -> dict[str, float]:
    """把权重按「参与评分的因子」重新归一化到和为 1（薄包装 `score_aisles` 的分母口径）。

    业务里这一口径是 `app/engine/scoring.score_aisles` 内联的：满分恒 1.0、有因子降级时
    满分仍是 1.0（跨轮次可比），降级因子权重**不进分母**，权重全 0 报 `ValueError`。
    这里把「分母 = 可用因子权重和」抽成可独立断言的形式，数值与业务一致（`17` §10.1）。
    """
    unavailable = set(degraded)
    factors = [factor for factor in weights if factor not in unavailable]
    denominator = sum(Decimal(str(weights[factor])) for factor in factors)
    if denominator == 0:
        raise ValueError("可用因子权重和为 0，无从归一化")
    return {
        factor: float(
            (Decimal(str(weights[factor])) / denominator).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
        )
        for factor in factors
    }


def select_degrade(
    aisles: Sequence[str], *, is_near_station: Mapping[str, bool | None]
) -> TierPlan:
    """把候选巷道分进四级降级链（re-export `resolve_tiers`）。"""
    return resolve_tiers(aisles, is_near_station=is_near_station)


def assert_no_pii(payload: Mapping[str, Any]) -> dict[str, Any]:
    """脱敏后断言不残留敏感字段：返回 `redact(payload)`，并校验禁出字段已被剥除。

    核心链路的出域护栏（20号 P0：核心链路出域事件 = 0）在评测侧的表达：给一个可能含
    敏感键的 payload，脱敏后任一 `FORBIDDEN_OUTBOUND_FIELDS` 键都不应出现。残留即
    `AssertionError`。
    """
    out = redact(payload)

    def _walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in FORBIDDEN_OUTBOUND_FIELDS:
                    raise AssertionError(f"脱敏后仍残留禁出字段：{key!r}")
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(out)
    return out


# ---------------------------------------------------------------------------
# L3 验收口径判定（18 §7.5 / 20号 Go-No-Go 闸门）—— run_evals 的 Go/No-Go 与本文件同源。
# ---------------------------------------------------------------------------


def attainment_rate(concentrations: Iterable[int], *, n_max: int = 5) -> float:
    """集中度达成率（18 §7.5）：N ≤ n_max 的出库单占比，目标 ≥70%。

    `concentrations` = 每张出库单的加权集中度 N（`weighted_concentration` 逐单结果）。
    空输入 → 0.0（零样本不是 100% 的虚高，与 `adoption_rate` 同口径）。
    """
    vals = list(concentrations)
    if not vals:
        return 0.0
    return sum(1 for n in vals if n <= n_max) / len(vals)


def judge_attainment(rate: float, *, min_rate: float = 0.70) -> bool:
    """达成率阈值判定（18 §7.5 / 20号 Go/No-Go）：rate ≥ min_rate → Go（True），否则 No-Go。"""
    return rate >= min_rate


def trend(current: float, previous: float, *, higher_is_better: bool = True) -> str:
    """趋势判定（20号 §7.5 趋势向好）：返回 `better` / `no_regression` / `worse`。

    `higher_is_better=True` 用于达成率 / 采纳率（越高越好）；`False` 用于跨巷道数 / 耗时
    （越低越好）。相等判 `no_regression`（不劣化也算过闸，`comparison: no_regression`）。
    """
    if current == previous:
        return "no_regression"
    is_better = current > previous if higher_is_better else current < previous
    return "better" if is_better else "worse"
