"""对话台统一入口 `POST /api/conversation/message`。D9 / D13。

事实来源：openspec/changes/ai-assist/design.md D9（L0/L2 意图 + write_intent）、
          D13（端点表：`conversation.operate` + 内部路由到 `ai.*`）
          spec `ai-assist`「对话台 L0/L2 意图识别」

本端点是四类冷路径能力的**统一入口**：L0 = 结构化（`intent` + `slots`）确定性路由，
L2 = 自由文本走外部 LLM NLU（脱敏后出站）。端点只做「识别意图 → 路由能力 → 落日志 →
双产物」四件事，**不写任何台账** —— 写意图（③④）只产出建议卡（影子模式 / 移库试算），
落地复用 `weight/apply` 与 `relocate.operate` 二次确认（红线 2「LLM 不直接执行写操作」）。

鉴权两层：端点级 `conversation.operate`（对话台本就有），加上 D13 的「内部路由到
`ai.*`」—— 识别出的能力再按各自权限拦一道（计划员对 `ai.*` 全无 → 403，即便它有
`conversation.operate`）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import current_account, get_db, require_cold_path_enabled, require_permission
from app.api.permissions import Permission, check
from app.core.config import settings
from app.core.errors import PermissionDenied, ValidationBlocked
from app.llm import DegradedReason, capabilities
from app.llm.intent import RecognizedIntent, route_intent
from app.llm.nlu import recognize_intent, redact_text
from app.models.configuration import Capability
from app.models.identity import Account
from app.models.llm import ConversationLog
from app.schemas.conversation import (
    ConversationMessageRequest,
    ConversationMessageResponse,
)

router = APIRouter(prefix="/api/conversation")

#: 能力 → 端点级 `ai.*` 权限（D13 的「内部路由到 `ai.assist`」落成这张表）。
#: ①② 只读建议与 ③ 建议归 `ai.assist`；④ 方案生成归 `ai.relocate.propose`。
_CAPABILITY_PERMISSION = {
    Capability.KPI_DIGEST: Permission.AI_ASSIST,
    Capability.DEVIATION_ATTRIBUTION: Permission.AI_ASSIST,
    Capability.WEIGHT_TUNING: Permission.AI_ASSIST,
    Capability.RELOCATE_PLAN: Permission.AI_RELOCATE_PROPOSE,
}


def _require_material_code(recognized: RecognizedIntent) -> str:
    """②④ 的 `material_code` 槽位必填（`route_intent` 只查键合法、不查必需 —— 见其 docstring）。"""
    material_code = recognized.slots.get("material_code")
    if not material_code:
        raise ValidationBlocked(
            f"意图 {recognized.intent} 需要槽位 material_code（如「把 3001234 收拢一下」）"
        )
    return material_code


def _dispatch(
    recognized: RecognizedIntent, session: Session, warehouse_id: str
) -> capabilities.DualProduct:
    """意图 → 能力调用。①② 只读、③ 影子模式、④ 试算 —— 均不写台账。"""
    cap = recognized.capability
    slots = recognized.slots
    if cap is Capability.KPI_DIGEST:
        return capabilities.kpi_interpret(
            session, warehouse_id=warehouse_id, settings=settings, period=slots.get("period")
        )
    if cap is Capability.DEVIATION_ATTRIBUTION:
        return capabilities.deviation_attribute(
            session,
            warehouse_id=warehouse_id,
            settings=settings,
            material_code=_require_material_code(recognized),
        )
    if cap is Capability.WEIGHT_TUNING:
        return capabilities.weight_tune(session, warehouse_id=warehouse_id, settings=settings)
    return capabilities.relocate_propose(
        session,
        warehouse_id=warehouse_id,
        settings=settings,
        material_code=_require_material_code(recognized),
    )


def _log_unrecognized(
    session: Session, *, account_id: int, warehouse_id: str, question: str
) -> None:
    """L2 NLU 识别失败时落一条「无意图」日志 —— 审计「数据出域 = 0」仍留痕。"""
    session.add(
        ConversationLog(
            warehouse_id=warehouse_id,
            account_id=account_id,
            intent=None,
            question_raw=question,
            question_redacted=redact_text(question),
            llm_output=None,
            hit_cold_path=False,
            slots_json=None,
        )
    )


@router.post("/message", response_model=ConversationMessageResponse)
def conversation_message(
    payload: ConversationMessageRequest,
    request: Request,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
    _conversation: None = Depends(require_permission(Permission.CONVERSATION_OPERATE)),
) -> ConversationMessageResponse:
    """对话台统一入口：识别意图（L0 结构化 / L2 脱敏 LLM）→ 路由能力 → 落日志 → 双产物。

    写意图（③④）只回建议 + `write_intent=true`，前端据此弹二次确认卡；落地走
    `weight/apply` / `relocate.operate`。本端点不写台账（红线 2）。
    """
    require_cold_path_enabled()

    # 1. 识别意图：L0 结构化（确定性路由）；L2 自由文本（脱敏后外部 LLM NLU）。
    if payload.intent is not None:
        recognized = route_intent(payload.intent, payload.slots)
        question_raw = payload.intent
        question_redacted = None
    else:
        question = payload.question or ""
        nlu = recognize_intent(question, settings=settings)
        if nlu is None:
            # L2 NLU 失败（Phase A `llm_provider=""`）→ fail-closed：不猜意图、不直出。
            _log_unrecognized(
                session,
                account_id=account.id,
                warehouse_id=payload.warehouse_id,
                question=question,
            )
            session.commit()
            return ConversationMessageResponse(
                intent=None,
                write_intent=False,
                rule={},
                ai=None,
                ai_generated=False,
                degraded_reason=DegradedReason.PROVIDER_UNCONFIGURED.value,
            )
        recognized = route_intent(nlu[0], nlu[1])
        question_raw = question
        question_redacted = redact_text(question)

    # 2. 内部路由 `ai.*` 鉴权（D13）：计划员对 `ai.*` 全无 → 403。
    required = _CAPABILITY_PERMISSION[recognized.capability]
    if not check(request.state.role, required):
        raise PermissionDenied(
            f"角色 {request.state.role.value} 无权执行 {required.value}",
            detail={"role": request.state.role.value, "permission": required.value},
        )

    # 3. 能力调用（只读 / 影子模式 / 试算，不写台账）。
    product = _dispatch(recognized, session, payload.warehouse_id)

    # 4. 落日志（非台账）：问句原文 / 脱敏文本 / LLM 产出 / 是否命中冷路径。
    #    `hit_cold_path` = 能力是否真调到了外部 LLM（`ai_generated`）；L2 的 NLU 调用
    #    在 Phase A 恒降级，接真实 provider 时再补 NLU 出站记账。
    session.add(
        ConversationLog(
            warehouse_id=payload.warehouse_id,
            account_id=account.id,
            intent=recognized.intent,
            question_raw=question_raw,
            question_redacted=question_redacted,
            llm_output=product.ai,
            hit_cold_path=product.ai_generated,
            slots_json=dict(recognized.slots) or None,
        )
    )
    session.commit()

    return ConversationMessageResponse(
        intent=recognized.intent,
        write_intent=recognized.write_intent,
        rule=product.rule,
        ai=product.ai,
        ai_generated=product.ai_generated,
        degraded_reason=product.degraded_reason,
    )
