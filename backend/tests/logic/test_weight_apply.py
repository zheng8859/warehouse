"""③ 采纳落地（`weight/apply`）契约测试（tasks.md 3.5 的验证）。

事实来源：openspec/changes/ai-assist/design.md D12（人采纳才生效，保留历史版本）
          spec `ai-assist`「③ 权重调优（≥50 批次，人采纳才生效）」

核心不变量：影子模式的 `AiSuggestion(PROPOSED)` **自身不写权重**；只有 `apply_weight`
（`ai.weight.update` 下）才新增一版 `WeightConfig`，且采纳的是 `context_json` 里
**规则算的拟采纳权重**（不是 LLM 叙事）—— 校验不过即阻断（红线 3）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.errors import NotFound, StateConflict, ValidationBlocked
from app.models.configuration import WEIGHT_FACTORS, Capability, WeightConfig
from app.models.llm import AiSuggestion, AiSuggestionStatus
from app.services.weight_tune import apply_weight
from tests.logic.conftest import DEFAULT_WEIGHTS, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 14, 10, 0)

#: 拟采纳权重：abc 调高、continuity 调低，与默认值（abc 0.25 / continuity 0.10）区分开。
_PROPOSED = {
    "abc": 0.30,
    "cap": 0.20,
    "existing": 0.15,
    "station": 0.20,
    "batch": 0.10,
    "continuity": 0.05,
}


def _proposed_suggestion(
    session: Session,
    *,
    weights: dict[str, Any] | None = None,
    capability: Capability = Capability.WEIGHT_TUNING,
) -> AiSuggestion:
    suggestion = AiSuggestion(
        warehouse_id=WAREHOUSE,
        capability_kind=capability,
        suggestion_text="建议文本（LLM 叙事）",
        status=AiSuggestionStatus.PROPOSED,
        context_json={
            "proposed_weights": weights if weights is not None else _PROPOSED,
            "current_weights": dict(DEFAULT_WEIGHTS),
            "sample_size": 50,
        },
    )
    session.add(suggestion)
    session.flush()
    return suggestion


def _versions(session: Session) -> list[WeightConfig]:
    return list(
        session.query(WeightConfig)
        .filter_by(warehouse_id=WAREHOUSE)
        .order_by(WeightConfig.version_no)
    )


# ------------------------------------------------------------------ 采纳才写 + 版本化（tasks 3.5 的验证）

def test_proposed_suggestion_alone_does_not_change_weights(session: Session) -> None:
    """未采纳不改权重：影子模式的 PROPOSED 建议自身不写 WeightConfig。"""
    make_scenario(session)
    _proposed_suggestion(session)

    assert [v.version_no for v in _versions(session)] == [1]


def test_apply_weight_writes_new_version_and_adopts(session: Session) -> None:
    """采纳才写：新一版 WeightConfig（version_no 递增），建议标记 ADOPTED。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session)

    config = apply_weight(
        session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW
    )

    assert config.version_no == 2
    assert config.effective_at == NOW
    assert config.weight_abc == pytest.approx(0.30)
    assert config.weight_continuity == pytest.approx(0.05)
    assert suggestion.status is AiSuggestionStatus.ADOPTED
    # 历史版本保留：两版并存，可回滚。
    assert [v.version_no for v in _versions(session)] == [1, 2]


def test_apply_weight_preserves_previous_version(session: Session) -> None:
    """版本化：旧版原样保留，不被改写。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session)
    apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    v1 = next(v for v in _versions(session) if v.version_no == 1)
    assert v1.weight_abc == pytest.approx(0.25)  # 默认权重 abc 0.25，未被新值覆盖。
    assert v1.weight_continuity == pytest.approx(0.10)


# ------------------------------------------------------------------ 规则校验与状态守卫

def test_apply_weight_rejects_repeat_adoption(session: Session) -> None:
    """已采纳不可重复采纳（StateConflict），且不因此多写一版。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session)
    apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    with pytest.raises(StateConflict):
        apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    assert [v.version_no for v in _versions(session)] == [1, 2]


def test_apply_weight_rejects_out_of_range_weight(session: Session) -> None:
    """规则校验：拟采纳权重超出 [0,1] → 阻断，不写权重、建议仍 PROPOSED。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(
        session, weights={**DEFAULT_WEIGHTS, "abc": 1.5}
    )

    with pytest.raises(ValidationBlocked):
        apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    assert [v.version_no for v in _versions(session)] == [1]
    assert suggestion.status is AiSuggestionStatus.PROPOSED


def test_apply_weight_rejects_wrong_key_set(session: Session) -> None:
    """规则校验：拟采纳权重键集不为六因子 → 阻断。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session, weights={"abc": 0.5})

    with pytest.raises(ValidationBlocked):
        apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    assert [v.version_no for v in _versions(session)] == [1]


def test_apply_weight_rejects_non_weight_tuning_suggestion(session: Session) -> None:
    """非权重调优建议不可经 weight/apply 采纳。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session, capability=Capability.KPI_DIGEST)

    with pytest.raises(ValidationBlocked):
        apply_weight(session, warehouse_id=WAREHOUSE, suggestion_id=suggestion.id, now=NOW)

    assert [v.version_no for v in _versions(session)] == [1]


def test_apply_weight_rejects_wrong_warehouse(session: Session) -> None:
    """仓库隔离：跨仓读建议 → NotFound。"""
    make_scenario(session)
    suggestion = _proposed_suggestion(session)

    with pytest.raises(NotFound):
        apply_weight(session, warehouse_id="OTHER", suggestion_id=suggestion.id, now=NOW)
