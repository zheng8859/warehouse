"""冷路径网关编排（统一链路 + 双产物响应）集成测试（tasks.md 2.4 的验证）。

事实来源：openspec/changes/ai-assist/design.md D4（双产物契约）
          spec `ai-assist`「双产物响应契约」「LLM 侧失败降级不报错」「成本护栏与熔断」

统一链路 = 脱敏 → 护栏 → 调用 → 记账 → 双产物。四种降级路径
（`provider_unconfigured` / `budget_exhausted` / `llm_timeout` / `llm_unavailable`）
都必须「200 + 规则卡片」：`rule` 恒有、`ai_generated=false`、`degraded_reason` 正确、
**不抛异常**（抛了就是 5xx）。成功路径 `ai_generated=true` 且同步记账。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm.capabilities import DualProduct, run_cold_path
from app.llm.quota import current_tokens, record_tokens

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
PERIOD = "2026-09"

RULE = {
    "material_code": "3001234",
    "concentration": 3.4,
    "cross_aisle_avg": 2.1,
    "adoption_rate": 0.72,
}


def _run(session: Session, settings: Settings, **overrides):
    kwargs = dict(
        settings=settings,
        session=session,
        warehouse_id=WAREHOUSE,
        period=PERIOD,
        rule=RULE,
    )
    kwargs.update(overrides)
    return run_cold_path(**kwargs)


# ------------------------------------------------------------------ 成功路径

def test_success_returns_dual_product_and_records_tokens(session: Session) -> None:
    """mock provider + 护栏未熔断 → rule 恒有、ai 非空、ai_generated=true、记账落库。"""
    settings = Settings(llm_provider="mock")

    result = _run(session, settings)

    assert result.rule == RULE
    assert result.ai is not None
    assert result.ai.startswith("[mock]")
    assert result.ai_generated is True
    assert result.degraded_reason is None
    assert current_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD) > 0


# ------------------------------------------------------------------ 四种降级路径（均 200 + 规则卡片，不抛异常）

def test_provider_unconfigured_degrades_with_rule_card(session: Session) -> None:
    """`llm_provider=""` → `provider_unconfigured`，规则卡片在、零记账、零调用。"""
    result = _run(session, Settings(llm_provider=""))

    assert result.rule == RULE
    assert result.ai is None
    assert result.ai_generated is False
    assert result.degraded_reason == "provider_unconfigured"
    assert current_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD) == 0


def test_budget_exhausted_circuit_breaks_with_rule_card(session: Session) -> None:
    """注入小预算 + 预记满额 → `budget_exhausted`，不追加记账、不调 LLM。"""
    settings = Settings(llm_provider="mock", llm_monthly_budget=100)
    record_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD, tokens=100)

    result = _run(session, settings)

    assert result.rule == RULE
    assert result.ai is None
    assert result.ai_generated is False
    assert result.degraded_reason == "budget_exhausted"
    # 熔断在记账之前：累计消费仍停在预记的 100。
    assert current_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD) == 100


def test_llm_timeout_degrades_with_rule_card(session: Session) -> None:
    """慢后端 + 短超时 → `llm_timeout`，规则卡片在、不抛异常。"""
    settings = Settings(llm_provider="mock")

    def slow(prompt: str) -> str:
        time.sleep(0.3)
        return "晚到"

    result = _run(session, settings, timeout_s=0.01, backend=slow)

    assert result.rule == RULE
    assert result.ai is None
    assert result.ai_generated is False
    assert result.degraded_reason == "llm_timeout"


def test_llm_unavailable_degrades_with_rule_card(session: Session) -> None:
    """未接线 provider（如 gemini）→ `llm_unavailable`，规则卡片在。"""
    result = _run(session, Settings(llm_provider="gemini"))

    assert result.rule == RULE
    assert result.ai is None
    assert result.ai_generated is False
    assert result.degraded_reason == "llm_unavailable"


# ------------------------------------------------------------------ 脱敏进链路

def test_outbound_prompt_is_redacted_before_the_call(session: Session) -> None:
    """脱敏在调用**之前**：mock 回显的提示词里不含 order_no / 操作员姓名。"""
    settings = Settings(llm_provider="mock")
    dirty_rule = {
        **RULE,
        "order_no": "PO-2026-007",
        "operator_name": "张三",
    }

    result = _run(session, settings, rule=dirty_rule)

    assert result.rule == dirty_rule                      # 规则卡片给用户，保留全量
    assert result.ai is not None
    assert "order_no" not in result.ai                     # 出境提示词已脱敏
    assert "operator_name" not in result.ai
    assert "3001234" in result.ai                          # 白名单字段还在


# ------------------------------------------------------------------ 不变量

def test_dual_product_enforces_the_invariant() -> None:
    """D4：`ai_generated == (degraded_reason is None)` —— 违反即构造失败。"""
    with pytest.raises(ValueError):
        DualProduct(rule={}, ai=None, ai_generated=True, degraded_reason="llm_timeout")
    with pytest.raises(ValueError):
        DualProduct(rule={}, ai="叙事", ai_generated=False, degraded_reason=None)
