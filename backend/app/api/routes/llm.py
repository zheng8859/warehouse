"""冷路径端点（D13 的 6 个 `POST /api/llm/*`）。D2 / D4 / D13。

事实来源：openspec/changes/ai-assist/design.md D2（开关守卫）、D4（双产物）、D13（端点表）
          spec `ai-assist`「冷路径开关守卫与一键关闭」「双产物响应契约」

6 个端点分三类：

- **开关**：`toggle`（`ai.toggle` 仅管理员）—— **不**挂开关守卫：它自己就是那道门，
  挂了就永远打不开（D2 的可纠正客户端错误，先开再调）。
- **只读 / 建议生成**：`kpi/interpret` `deviation/attribute` `weight/tune`
  （`ai.assist`）、`relocate/propose`（`ai.relocate.propose`）—— 受开关守卫，双产物。
- **采纳落地**：`weight/apply`（`ai.weight.update`）—— 规则校验写 `WeightConfig`
  （`services/weight_tune.apply_weight`），无 LLM 叙事，非双产物。

开关守卫（`_require_cold_path_enabled`）声明在权限依赖**之前**：开关关闭时任何角色都
得 409 `cold_path_disabled`（spec「任意角色 → 409」）—— 先回答「功能开没开」，再回答
「你够不够格」。`toggle` 之外的五端点都挂它。

红线 2（LLM 不直接执行写操作）在这里的落点：`weight/apply` 与 `relocate/propose` 都不
写台账；`weight/apply` 采纳的是规则算的拟采纳权重（红线 3），`relocate/propose` 只试算、
落地复用 `relocate.operate` 二次确认。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_account, get_db, require_permission
from app.api.permissions import Permission
from app.core.config import settings
from app.core.errors import ColdPathDisabled
from app.llm import capabilities
from app.models.configuration import WEIGHT_FACTORS
from app.models.identity import Account
from app.schemas.llm import (
    DeviationAttributeRequest,
    DualProductResponse,
    KpiInterpretRequest,
    RelocateProposeRequest,
    ToggleRequest,
    ToggleResponse,
    WeightApplyRequest,
    WeightApplyResponse,
    WeightTuneRequest,
)
from app.services.weight_tune import apply_weight

router = APIRouter(prefix="/api/llm")


def _require_cold_path_enabled() -> None:
    """开关守卫（D2）：关闭（默认）→ 409 `cold_path_disabled`。`toggle` 不挂此依赖。"""
    if not settings.cold_path_enabled:
        raise ColdPathDisabled(
            "冷路径未开启（cold_path_enabled=false）—— 请管理员先经 POST /api/llm/toggle 开启"
        )


def _dual(product: capabilities.DualProduct) -> DualProductResponse:
    """网关 `DualProduct` → 响应 DTO（纯搬运，不重复校验不变量）。"""
    return DualProductResponse(
        rule=product.rule,
        ai=product.ai,
        ai_generated=product.ai_generated,
        degraded_reason=product.degraded_reason,
    )


@router.post("/toggle", response_model=ToggleResponse)
def toggle(
    payload: ToggleRequest,
    _perm: None = Depends(require_permission(Permission.AI_TOGGLE)),
) -> ToggleResponse:
    """冷路径开关（`ai.toggle` 仅管理员）。运行时改 `settings.cold_path_enabled`。"""
    settings.cold_path_enabled = payload.enabled
    return ToggleResponse(cold_path_enabled=settings.cold_path_enabled)


@router.post("/kpi/interpret", response_model=DualProductResponse)
def kpi_interpret(
    payload: KpiInterpretRequest,
    session: Session = Depends(get_db),
    _cold: None = Depends(_require_cold_path_enabled),
    _perm: None = Depends(require_permission(Permission.AI_ASSIST)),
) -> DualProductResponse:
    """① KPI 解读（只读，`ai.assist`）：规则聚合 → 网关 → 双产物。"""
    product = capabilities.kpi_interpret(
        session, warehouse_id=payload.warehouse_id, settings=settings, period=payload.period
    )
    session.commit()
    return _dual(product)


@router.post("/deviation/attribute", response_model=DualProductResponse)
def deviation_attribute(
    payload: DeviationAttributeRequest,
    session: Session = Depends(get_db),
    _cold: None = Depends(_require_cold_path_enabled),
    _perm: None = Depends(require_permission(Permission.AI_ASSIST)),
) -> DualProductResponse:
    """② 偏离归因（只读，`ai.assist`）：规则侧多源明细 → 网关 → 双产物。"""
    product = capabilities.deviation_attribute(
        session,
        warehouse_id=payload.warehouse_id,
        material_code=payload.material_code,
        batch_no=payload.batch_no,
        settings=settings,
    )
    session.commit()
    return _dual(product)


@router.post("/weight/tune", response_model=DualProductResponse)
def weight_tune(
    payload: WeightTuneRequest,
    session: Session = Depends(get_db),
    _cold: None = Depends(_require_cold_path_enabled),
    _perm: None = Depends(require_permission(Permission.AI_ASSIST)),
) -> DualProductResponse:
    """③ 权重调优建议（`ai.assist`）：反事实模拟规则算 + 影子模式（PROPOSED）→ 双产物。

    落地 `session.commit()`：把 PROPOSED 建议落库，`weight/apply` 才能按 `suggestion_id`
    采纳它（不提交的话，`get_db` 关闭会话即回滚，建议随之消失）。
    """
    product = capabilities.weight_tune(session, warehouse_id=payload.warehouse_id, settings=settings)
    session.commit()
    return _dual(product)


@router.post("/weight/apply", response_model=WeightApplyResponse)
def weight_apply(
    payload: WeightApplyRequest,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
    _cold: None = Depends(_require_cold_path_enabled),
    _perm: None = Depends(require_permission(Permission.AI_WEIGHT_UPDATE)),
) -> WeightApplyResponse:
    """③ 采纳落地（`ai.weight.update`）：规则校验拟采纳权重 → 写新版 `WeightConfig`。

    规则侧（`services/weight_tune.apply_weight`），无 LLM 叙事 —— 采纳的是建议里
    **规则算的**权重，不是 LLM 文本（红线 3）。`changed_by_id` = 采纳人。
    """
    config = apply_weight(
        session,
        warehouse_id=payload.warehouse_id,
        suggestion_id=payload.suggestion_id,
        changed_by_id=account.id,
    )
    session.commit()
    return WeightApplyResponse(
        suggestion_id=payload.suggestion_id,
        version_no=config.version_no,
        weights={factor: float(getattr(config, f"weight_{factor}")) for factor in WEIGHT_FACTORS},
    )


@router.post("/relocate/propose", response_model=DualProductResponse)
def relocate_propose(
    payload: RelocateProposeRequest,
    session: Session = Depends(get_db),
    _cold: None = Depends(_require_cold_path_enabled),
    _perm: None = Depends(require_permission(Permission.AI_RELOCATE_PROPOSE)),
) -> DualProductResponse:
    """④ 移库方案（只读，`ai.relocate.propose`）：规则算多方案 + 三重校验 → 双产物。

    试算不落库、不写 `JobOrder`；落地复用 `relocate.operate` 二次确认（红线 2）。
    """
    product = capabilities.relocate_propose(
        session, warehouse_id=payload.warehouse_id, material_code=payload.material_code, settings=settings
    )
    session.commit()
    return _dual(product)
