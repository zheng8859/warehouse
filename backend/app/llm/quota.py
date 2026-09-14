"""成本三道护栏：单请求 token 上限、并发上限、月度预算硬上限（超限熔断降级）。10 §七。

事实来源：10-AI 辅助能力（冷路径）设计 §七（成本护栏三门槛）
          openspec/changes/ai-assist/design.md D3（默认值 + 同步记账）
          spec `ai-assist`「成本护栏与熔断」

三道护栏在**请求入口**即时拦截（D3），顺序有讲究：

1. **月度预算**（`llm_monthly_budget`）—— 最便宜、最该先判的：耗尽 = 熔断
   `budget_exhausted`，降级为「仅规则卡片」。无副作用。
2. **单请求 token 上限**（`llm_max_tokens_per_req`）—— 超限**拒绝并提示拆分**，不截断
   文本（截断会让「规则算的数」被砍半，产出不可信）。无副作用。
3. **并发上限**（`llm_max_concurrency`）—— 最后判，因为它是唯一有副作用的（`acquire`
   占名额，调用方必须在调用后 `release`）。

`estimate_tokens` 是 Phase A 的 token 代理：真实 tokenizer 属外部依赖，本阶段用
「字符数 // 4 向上取整」的确定性代理。它与 `llm_max_tokens_per_req` / `llm_monthly_budget`
同单位，所以「超上限」「达预算」的判定直接可比、可直接测。接线真实 tokenizer 时只换
这一个函数，护栏逻辑不动。

记账同步落 `AiCostQuota`（一行 = 一个仓库一个月的累计消费），`record_tokens` 原地累加
并刷新 `updated_at`。落库不落内存（D7）：重启不丢、月预算可审计。
"""
from __future__ import annotations

import threading
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.base import utcnow
from app.models.llm import AiCostQuota


class QuotaVerdict(str, Enum):
    """入口检查结果。`ALLOW` 已占并发名额（调用方须 `release`）；其余无副作用。"""

    ALLOW = "allow"
    BUDGET_EXHAUSTED = "budget_exhausted"          # → 降级 budget_exhausted（仅规则卡片）
    TOKENS_EXCEEDED = "tokens_exceeded"            # → 拒绝并提示拆分
    CONCURRENCY_SATURATED = "concurrency_saturated"  # → 排队或拒绝，不突破并发上限


def estimate_tokens(text: str) -> int:
    """token 代理：字符数 // 4 向上取整（Phase A 无真实 tokenizer）。"""
    return max(1, (len(text) + 3) // 4)


class ConcurrencyGate:
    """在途 LLM 调用计数门。单进程内用锁保证原子，不突破 `limit`。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self._limit:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1


def check_guardrails(
    *,
    settings: Settings,
    prompt: str,
    tokens_consumed: int,
    gate: ConcurrencyGate,
) -> QuotaVerdict:
    """三道护栏入口检查（顺序见模块 docstring）。"""
    if tokens_consumed >= settings.llm_monthly_budget:
        return QuotaVerdict.BUDGET_EXHAUSTED
    if estimate_tokens(prompt) > settings.llm_max_tokens_per_req:
        return QuotaVerdict.TOKENS_EXCEEDED
    if not gate.try_acquire():
        return QuotaVerdict.CONCURRENCY_SATURATED
    return QuotaVerdict.ALLOW


def _row(session: Session, *, warehouse_id: str, period: str) -> AiCostQuota | None:
    return session.scalars(
        sa.select(AiCostQuota).where(
            AiCostQuota.warehouse_id == warehouse_id,
            AiCostQuota.period == period,
        )
    ).one_or_none()


def current_tokens(session: Session, *, warehouse_id: str, period: str) -> int:
    """本仓库本周期累计消费；尚无记账行 = 0。"""
    row = _row(session, warehouse_id=warehouse_id, period=period)
    return 0 if row is None else row.tokens_consumed


def record_tokens(
    session: Session, *, warehouse_id: str, period: str, tokens: int
) -> int:
    """同步记账：`tokens_consumed` 原地累加、`updated_at` 刷新，返回新累计值。"""
    row = _row(session, warehouse_id=warehouse_id, period=period)
    if row is None:
        # 显式给 0：列 default=0 只在 INSERT 时由 SQLAlchemy 落库，实例化瞬间
        # `tokens_consumed` 还是 None，直接 += 会炸。
        row = AiCostQuota(warehouse_id=warehouse_id, period=period, tokens_consumed=0)
        session.add(row)
    row.tokens_consumed += tokens
    row.updated_at = utcnow()
    session.flush()
    return row.tokens_consumed
