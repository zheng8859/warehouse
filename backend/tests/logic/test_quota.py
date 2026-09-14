"""成本护栏三门槛 + 同步记账（tasks.md 2.3 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §七（成本护栏三门槛）
          openspec/changes/ai-assist/design.md D3（token/并发/月预算 + 同步记账）
          spec `ai-assist`「成本护栏与熔断」

三门槛：① 单请求 token 上限（超限拒绝并提示拆分，不截断）② 并发上限（在途不突破）
③ 月度预算硬上限（耗尽入口熔断 `budget_exhausted`）。记账同步落 `AiCostQuota`。
「注入小预算触发熔断」「并发计数」「同步记账」三者是本任务的验证点。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.llm.quota import (
    ConcurrencyGate,
    QuotaVerdict,
    check_guardrails,
    current_tokens,
    estimate_tokens,
    record_tokens,
)
from app.models.llm import AiCostQuota

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
PERIOD = "2026-09"


# ------------------------------------------------------------------ token 代理

def test_estimate_tokens_is_deterministic_and_positive() -> None:
    """Phase A 无真实 tokenizer：字符数 // 4 向上取整的确定性代理（同输入同输出）。"""
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 40) == 10
    assert estimate_tokens("上周集中度怎么样") == estimate_tokens("上周集中度怎么样")


# ------------------------------------------------------------------ 三道护栏入口

def test_budget_exhausted_circuit_breaks_at_entry() -> None:
    """注入小预算：`consumed >= llm_monthly_budget` 时入口熔断，且**不占并发名额**。"""
    settings = Settings(llm_monthly_budget=100)
    gate = ConcurrencyGate(4)

    verdict = check_guardrails(
        settings=settings,
        prompt="x" * 40,
        tokens_consumed=100,
        gate=gate,
    )

    assert verdict is QuotaVerdict.BUDGET_EXHAUSTED
    assert gate.in_flight == 0  # 熔断发生在 acquire 之前，无需 release


def test_oversized_request_rejected_not_truncated() -> None:
    """单请求超 token 上限 → 拒绝（`TOKENS_EXCEEDED`），不是截断文本。"""
    settings = Settings(llm_max_tokens_per_req=10)
    gate = ConcurrencyGate(4)

    verdict = check_guardrails(
        settings=settings,
        prompt="x" * 200,          # estimate_tokens = 50 > 10
        tokens_consumed=0,
        gate=gate,
    )

    assert verdict is QuotaVerdict.TOKENS_EXCEEDED
    assert gate.in_flight == 0


def test_concurrency_gate_blocks_at_limit_and_recovers_on_release() -> None:
    """并发计数：到上限拒绝，release 后恢复。"""
    gate = ConcurrencyGate(2)

    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.in_flight == 2
    assert gate.try_acquire() is False          # 第 3 个在途被拒

    gate.release()
    assert gate.in_flight == 1
    assert gate.try_acquire() is True           # 空出一个名额即可进


def test_check_guardrails_acquires_slot_on_allow() -> None:
    """ALLOW 才有副作用（占并发名额）；调用方随后必须 release。"""
    settings = Settings(llm_monthly_budget=1_000_000, llm_max_tokens_per_req=4096)
    gate = ConcurrencyGate(4)

    verdict = check_guardrails(
        settings=settings,
        prompt="x" * 40,
        tokens_consumed=0,
        gate=gate,
    )

    assert verdict is QuotaVerdict.ALLOW
    assert gate.in_flight == 1


# ------------------------------------------------------------------ 同步记账

def test_record_tokens_creates_row_then_accumulates_in_place(session: Session) -> None:
    """同步记账：首次建行、后续**原地累加**同一行（唯一约束 `(warehouse_id, period)`）。"""
    first = record_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD, tokens=1200)
    second = record_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD, tokens=800)

    assert first == 1200
    assert second == 2000
    rows = session.scalars(
        sa.select(AiCostQuota).where(
            AiCostQuota.warehouse_id == WAREHOUSE, AiCostQuota.period == PERIOD
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].tokens_consumed == 2000


def test_record_tokens_refreshes_updated_at(session: Session) -> None:
    """`updated_at` 是「最后一次记账时间」—— 每次累加都要刷新（D7）。"""
    record_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD, tokens=100)
    row = session.scalars(
        sa.select(AiCostQuota).where(
            AiCostQuota.warehouse_id == WAREHOUSE, AiCostQuota.period == PERIOD
        )
    ).one()
    before = row.updated_at

    record_tokens(session, warehouse_id=WAREHOUSE, period=PERIOD, tokens=100)
    session.expire_all()

    after = session.scalars(
        sa.select(AiCostQuota).where(
            AiCostQuota.warehouse_id == WAREHOUSE, AiCostQuota.period == PERIOD
        )
    ).one().updated_at
    assert after >= before


def test_current_tokens_reads_zero_when_no_row(session: Session) -> None:
    """尚无记账行 → 累计消费为 0（不是 KeyError / None）。"""
    assert current_tokens(session, warehouse_id=WAREHOUSE, period="2026-11") == 0
