"""冷路径链 3 实体的契约测试（tasks.md 1.2 的验证）。

事实来源：10-AI 辅助能力（冷路径）设计 §七、15-05 §4.1、29 号
          openspec/changes/ai-assist/design.md D7（3 冷路径实体，非台账）
          spec `data-model`「实体清单与数据链分组」「冷路径实体非台账」

取值来源：能力枚举取自 `configuration.Capability`（与 `PromptTemplate` 同源）；
建议状态、配额周期、问句原文是**合成值**（文档未给样本，已在用例注明）。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.enums import AccountStatus, Role
from app.models.base import Base
from app.models.configuration import Capability
from app.models.identity import Account
from app.models.llm import (
    AiCostQuota,
    AiSuggestion,
    AiSuggestionStatus,
    ConversationLog,
)

pytestmark = pytest.mark.model

WAREHOUSE = "GTJ10036"
NOW = "2026-09-14 12:00:00"


def _account(session: Session, **overrides) -> Account:
    fields = {
        "warehouse_id": WAREHOUSE,
        "username": "gtj_keeper",
        "password_hash": "$2b$12$" + "0123456789abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOP",
        "role": Role.WAREHOUSE_KEEPER,
        "status": AccountStatus.ACTIVE,
    }
    fields.update(overrides)
    row = Account(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _suggestion(session: Session, **overrides) -> AiSuggestion:
    fields = {
        "warehouse_id": WAREHOUSE,
        "capability_kind": Capability.KPI_DIGEST,
        "suggestion_text": "上周集中度 3.4，接近达标。",
    }
    fields.update(overrides)
    row = AiSuggestion(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _raw_insert(session: Session, table: str, **values) -> None:
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    session.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values)


# ------------------------------------------------------------------ 冷路径实体非台账

def test_cold_path_tables_carry_the_isolation_column() -> None:
    """spec「冷路径实体非台账」+ §十一：三者都带 `warehouse_id` 隔离。"""
    for table_name in ("ai_suggestions", "conversation_logs", "ai_cost_quotas"):
        assert "warehouse_id" in Base.metadata.tables[table_name].c.keys()


def test_cold_path_entities_have_no_soft_delete_column() -> None:
    """17 §十二「归档不删除 + 版本化」：不引入 `is_deleted`（tasks 1.2 的无 is_deleted 断言）。"""
    for table_name in ("ai_suggestions", "conversation_logs", "ai_cost_quotas"):
        columns = set(Base.metadata.tables[table_name].c.keys())
        assert "is_deleted" not in columns
        assert "deleted_at" not in columns


def test_cold_path_entities_do_not_reference_the_ledger_chain() -> None:
    """spec「冷路径实体非台账」的结构侧：三表都没有指向 `ledgers` 的外键。

    台账只有一套（`Ledger`，CLAUDE.md §四），冷路径链不得借「建议」「日志」「配额」
    的名义另起一条写台账的旁路。
    """
    for table_name in ("ai_suggestions", "conversation_logs", "ai_cost_quotas"):
        for column in Base.metadata.tables[table_name].c:
            for fk in column.foreign_keys:
                assert fk.target_fullname.split(".")[0] != "ledgers", (
                    f"{table_name}.{column.name} 指向了台账表"
                )


# ------------------------------------------------------------------ AiSuggestion：CRUD 与默认值

def test_suggestion_defaults_to_proposed_shadow_mode(session: Session) -> None:
    """D12「建议默认影子模式」：新建议的采纳状态 = `PROPOSED`（不生效）。"""
    row = _suggestion(session)
    assert row.status is AiSuggestionStatus.PROPOSED


def test_suggestion_round_trips_context_json(session: Session) -> None:
    """`context_json` 是规则侧结构化数据（③ 的拟采纳权重等），读回是 dict 不是字符串。"""
    row = _suggestion(
        session,
        capability_kind=Capability.WEIGHT_TUNING,
        context_json={"weights": {"continuity": 0.05, "station": 0.25}},
    )
    session.flush()
    session.expire_all()

    fresh = session.get(AiSuggestion, row.id)
    assert isinstance(fresh.context_json, dict)  # type: ignore[union-attr]
    assert fresh.context_json["weights"]["station"] == 0.25  # type: ignore[union-attr]


def test_suggestion_capability_is_the_four_cold_path_capabilities(session: Session) -> None:
    """`capability_kind` 与 `PromptTemplate.capability` 同一 `Capability` 值域（四类）。"""
    assert {c.value for c in Capability} == {"KPI 解读", "偏离归因", "权重调优", "移库方案"}

    for capability in Capability:
        row = _suggestion(session, capability_kind=capability)
        assert row.capability_kind is capability


def test_suggestion_status_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层：非枚举取值在绑定参数时抛 `StatementError`。"""
    with pytest.raises(StatementError):
        _suggestion(session, status="待采纳")


# ------------------------------------------------------------------ ConversationLog：CRUD 与脱敏语义

def test_conversation_log_attributes_the_asking_account(session: Session) -> None:
    """问句可追责：`account_id` 指向账号（与 `ConversationContext` 同一处置）。"""
    acct = _account(session)
    row = ConversationLog(
        warehouse_id=WAREHOUSE,
        account_id=acct.id,
        question_raw="把 3001234 收拢一下",
    )
    session.add(row)
    session.flush()
    session.expire_all()

    fresh = session.get(ConversationLog, row.id)
    assert fresh.account_id == acct.id  # type: ignore[union-attr]
    assert fresh.hit_cold_path is False  # type: ignore[union-attr]  默认未命中冷路径


def test_conversation_log_requires_an_existing_account(session: Session) -> None:
    """外键生效（`foreign_keys=ON`）：`account_id` 不能指向不存在的账号。"""
    row = ConversationLog(
        warehouse_id=WAREHOUSE,
        account_id=999_999,
        question_raw="x",
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_conversation_log_keeps_raw_and_redacted_distinct(session: Session) -> None:
    """问句原文（未脱敏，永不出站）与脱敏后文本是**两列**，不是覆盖写。

    出站只走 `question_redacted`（`llm/redact.py`），原文留在库内可审计。
    """
    acct = _account(session)
    row = ConversationLog(
        warehouse_id=WAREHOUSE,
        account_id=acct.id,
        question_raw="3001234 为什么跨了 11 个巷道？操作员张三备注：担心排队",
        question_redacted="料号 3001234 跨巷道 11，排查项：容量/降级",
        llm_output="可能原因：容量、降级。",
        hit_cold_path=True,
        intent="DEVIATION_ATTRIBUTE",
        slots_json={"material_code": "3001234"},
    )
    session.add(row)
    session.flush()
    session.expire_all()

    fresh = session.get(ConversationLog, row.id)
    assert fresh.question_raw != fresh.question_redacted  # type: ignore[union-attr]
    assert "操作员" in fresh.question_raw  # type: ignore[union-attr]
    assert "操作员" not in fresh.question_redacted  # type: ignore[union-attr]
    assert fresh.hit_cold_path is True  # type: ignore[union-attr]


# ------------------------------------------------------------------ AiCostQuota：唯一约束与记账

def test_one_quota_row_per_warehouse_per_period(session: Session) -> None:
    """一个仓库一个预算周期只有一行 —— 否则「本月累计」要跨行求和，熔断阈值会对不上。"""
    session.add(AiCostQuota(warehouse_id=WAREHOUSE, period="2026-09"))
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(AiCostQuota(warehouse_id=WAREHOUSE, period="2026-09"))
        session.flush()
    session.rollback()


def test_quota_unique_key_starts_with_the_warehouse() -> None:
    """唯一键 = `(warehouse_id, period)`，隔离维度在前。"""
    constraints = [
        c
        for c in Base.metadata.tables["ai_cost_quotas"].constraints
        if isinstance(c, sa.UniqueConstraint)
    ]
    assert len(constraints) == 1
    assert list(constraints[0].columns.keys()) == ["warehouse_id", "period"]


def test_quota_accumulates_tokens_in_place(session: Session) -> None:
    """同步记账是**原地累加**同一行，不是每次新增一行。"""
    session.add(AiCostQuota(warehouse_id=WAREHOUSE, period="2026-09"))
    session.flush()
    row = session.scalars(
        sa.select(AiCostQuota).where(
            AiCostQuota.warehouse_id == WAREHOUSE, AiCostQuota.period == "2026-09"
        )
    ).one()
    row.tokens_consumed += 1200
    session.flush()
    session.expire_all()

    assert session.execute(text("SELECT count(*) FROM ai_cost_quotas")).scalar_one() == 1
    assert session.get(AiCostQuota, row.id).tokens_consumed == 1200  # type: ignore[union-attr]


def test_quota_tokens_consumed_defaults_to_zero(session: Session) -> None:
    """新周期行从 0 起算。"""
    session.add(AiCostQuota(warehouse_id=WAREHOUSE, period="2026-10"))
    session.flush()
    row = session.scalars(
        sa.select(AiCostQuota).where(AiCostQuota.period == "2026-10")
    ).one()
    assert row.tokens_consumed == 0


def test_quota_tokens_consumed_negative_rejected_by_db_check(session: Session) -> None:
    """负消费会让「是否已达预算」的判定方向反掉，DB CHECK 挡第二层。"""
    with pytest.raises(IntegrityError):
        _raw_insert(
            session,
            "ai_cost_quotas",
            warehouse_id="'" + WAREHOUSE + "'",
            period="'2026-09'",
            tokens_consumed="-1",
            updated_at="'" + NOW + "'",
            created_at="'" + NOW + "'",
        )
    session.rollback()


# ------------------------------------------------------------------ 局部值域不进共享枚举

def test_suggestion_status_is_an_entity_local_domain() -> None:
    """D2：建议状态是实体局部值域，不进 `enums.py`（共享枚举仍 11 个）。"""
    assert AiSuggestionStatus.__module__ == "app.models.llm"
    assert not hasattr(shared_enums, "AiSuggestionStatus")
