"""作业单写操作与验证读 路由（`design.md` D6 的端点清单）。

事实来源：15-入库出库移库与后验流程设计 §6.2/§6.3（逐单处置与写台账）、§7（后验）
          17-数据模型设计 §4.1（作业单标识）、§4.3（台账）、§4.4（后验 / 偏离）
          openspec/changes/transaction-base/design.md D6（端点与 RBAC）、D7（逐单独立提交）
          spec `transaction-base`「作业单写操作端点」「台账与后验验证读」

## 端点清单（D6）

| 方法 | 路径 | 动作 | 服务编排 |
|---|---|---|---|
| POST | `/api/job/batch/confirm` | 批量确认（逐单独立事务） | `confirm_*` |
| POST | `/api/job/{id}/reject` | 驳回（`PLANNED → REJECTED`） | 状态机 |
| POST | `/api/job/{id}/retry` | 后验重试（`VERIFY_FAILED → VERIFYING → …`） | `run_verification` |
| POST | `/api/job/{id}/void` | 冲正（`EXECUTED/VERIFIED → VOID`） | `void_job` |
| GET  | `/api/jobs` | 按类型查作业队列（入库 p3 多选队列入口） | — |
| GET  | `/api/plan/{plan_id}` | 按方案取推荐理由体（`payload_json`） | — |
| GET  | `/api/ledger` | 按作业单查台账（含反向行） | — |
| GET  | `/api/verification/{job_id}` | 查某单的后验结果 | — |
| GET  | `/api/deviation` | 查本仓偏离批次清单（移库任务来源） | `kpi.list_deviations` |

## 端点 = 组装点，不是编排本身

与 `allocate.py` 同一立场：端点不评一个分、不写一条台账 —— 编排在 `services/confirm.py` /
`verify.py` / `void.py`，端点只做「报文 → 可查的 id → 调编排 → 划定事务边界 → 组装响应」。
「同样输入必得同样输出」的可审性依赖这条边界：`now` 在这里取一次，其余全部注入编排。

## 两个「逐单」要分清（D7）

1. **批量确认的「逐单独立提交」**：`batch/confirm` 对每一张单 `confirm_*` 之后**各自
   `commit()`**，个别单失败（写台账失败回 `PLANNED`、源状态非 `PLANNED` 被拒）不影响
   同批其余单 —— 不是整批一次提交。这是 `design.md` D7 的明文口径，与 `allocate` 的
   「整批一次提交」正好相反（那边要的是「一半有方案一半没有的中间态不存在」）。
2. **`deps.get_db` 的「不提交」**：事务边界属于端点，`get_db` 只回滚不提交。两者不矛盾 ——
   「逐单独立提交」的边界在这里（端点内），`get_db` 负责「异常时整体回滚」。

## v1 不做端点级 RBAC（D6）

`POST /api/job/*` 的写操作**不挂 `require_permission`**：二次确认是前端确认卡，写端点本身
**就是**操作员确认的入口（`design.md` D6 明文「v1 不加端点级 RBAC」）。认证仍由全局中间件
覆盖（401），资源级鉴权属路线图 RBAC（M5）。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import current_account, get_db, require_permission
from app.api.permissions import Permission
from app.core.concurrency import bump_lock_version
from app.core.enums import AbcClass, JobStatus, JobType
from app.core.errors import BlockedMissingPrerequisite, StateConflict, ValidationBlocked
from app.core.state_machine import assert_transition
from app.engine.factors import load_snapshot_index
from app.engine.reasons import load_bulk_batch_nos, next_bulk_batch_no
from app.models.identity import Account
from app.models.job import (
    DeviationStatus,
    JobOrder,
    Ledger,
    PlanKind,
    RecommendationPlan,
    Verification,
)
from app.models.linkage import Snapshot
from app.schemas.job import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    ConfirmItem,
    ConfirmOutcome,
    DeviationItem,
    JobQueueItem,
    JobStatusResponse,
    LedgerItem,
    RejectRequest,
    RetryRequest,
    VerificationItem,
    VoidRequest,
)
from app.schemas.reason import (
    MAX_JOB_ORDERS_PER_BATCH,
    BatchPickSequenceRequest,
    BatchPickSequenceResponse,
    PickPlanItem,
)
from app.services import kpi
from app.services.inbound import confirm_inbound
from app.services.outbound import confirm_outbound, derive_pick_sequence
from app.services.relocate import confirm_relocate
from app.services.verify import run_verification
from app.services.void import void_job

router = APIRouter(prefix="/api/job")

#: 验证读挂在 `/api` 顶层（`design.md` D6 明文：`GET /api/ledger`、`GET /api/verification/{job_id}`），
#: 不挂 `/api/job/*` 写前缀 —— 台账 / 后验是独立资源（一套台账、逐单后验），读路径与
#: 作业单写路径分开，避免把「查台账」误读成「某个作业单的子资源」。
reads_router = APIRouter(prefix="/api")

#: 作业单标识的线上形态：`str(JobOrder.id)` —— 十进制正整数、无前导零、无符号。
#: 与 `allocate.py` 的 `_ID_PATTERN` 同一约定（见 `schemas/job.py` 模块 docstring 第 1 条）。
_ID_PATTERN = re.compile(r"^[1-9]\d*$")


def _parse_ids(raw_ids: Sequence[str]) -> tuple[int, ...]:
    """报文里的单号 → 整数 id；「写法不合法」与「重号」在这里拦成 422。

    与 `allocate._requested_ids` 同一原则：两者都是**报文错误**，不留给编排去抛裸
    `ValueError`（那会变 500）。重号必须拦 —— 同一张单在同一批确认里出现两次，第二次
    撞的是状态守卫（`StateConflict`），但那是「源状态错」不是「报文错」，语义被改写。
    """
    ids: list[int] = []
    for raw in raw_ids:
        if not _ID_PATTERN.match(raw):
            raise ValidationBlocked(
                f"作业单标识 {raw!r} 的形状不合法 —— 本接口用 str(JobOrder.id)"
                "（十进制正整数、无前导零）",
                detail={"job_order_id": raw},
            )
        ids.append(int(raw))

    duplicated = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicated:
        raise ValidationBlocked(
            f"作业单标识重复 {duplicated} —— 同一张单在同一批里只能确认一次",
            detail={"duplicated_job_order_ids": duplicated},
        )
    return tuple(ids)


def _load_orders(
    session: Session, *, warehouse_id: str, ids: Sequence[int]
) -> dict[int, JobOrder]:
    """按 id 取本仓的作业单；少一条即**整批**拒绝（与 `allocate._load_orders` 同一理由）。

    `warehouse_id` 必须进查询（`CLAUDE.md` §七 数据隔离）：别的仓的单在本接口里就是
    **未知单号**。返回 `{id: JobOrder}` 字典，便于批量确认按请求序逐条取。
    """
    orders = {
        order.id: order
        for order in session.scalars(
            sa.select(JobOrder).where(
                JobOrder.warehouse_id == warehouse_id, JobOrder.id.in_(ids)
            )
        )
    }
    missing = [value for value in ids if value not in orders]
    if missing:
        raise ValidationBlocked(
            f"作业单不存在或不属于本仓（{warehouse_id}）：{missing}",
            detail={"missing_job_order_ids": missing},
        )
    return orders


def _load_order(session: Session, *, warehouse_id: str, job_id: str) -> JobOrder:
    """单作业单动作（reject / retry / void / 读）的取单：形状 + 存在 + 归属一并校验。"""
    if not _ID_PATTERN.match(job_id):
        raise ValidationBlocked(
            f"作业单标识 {job_id!r} 的形状不合法 —— 本接口用 str(JobOrder.id)",
            detail={"job_order_id": job_id},
        )
    order = session.scalars(
        sa.select(JobOrder).where(
            JobOrder.warehouse_id == warehouse_id, JobOrder.id == int(job_id)
        )
    ).first()
    if order is None:
        raise ValidationBlocked(
            f"作业单不存在或不属于本仓（{warehouse_id}）：{job_id}",
            detail={"missing_job_order_ids": [int(job_id)]},
        )
    return order


def _current_snapshot_or_none(session: Session, *, warehouse_id: str) -> Snapshot | None:
    """本仓当前快照基线（`version_no` 最大者）；一版都没有返回 `None`。

    与 `allocate._current_snapshot` 的不同只在「一版都没有」的处置：分配要拿
    `snapshot_version` 渲染响应，故**阻断**；确认 / 冲正 / 后验把 `None` 交给编排 ——
    `apply_increment` 对 `snapshot=None` 是 no-op、`_confirm_and_execute` / `run_verification`
    遇到快照缺失**阻断**（`BlockedMissingPrerequisite`，409，不迁 `VERIFY_FAILED` ——
    快照缺失是「没算」不是「达标」，`CLAUDE.md` §四）。两处登记见 `design.md`。
    """
    return session.scalars(
        sa.select(Snapshot)
        .where(Snapshot.warehouse_id == warehouse_id)
        .order_by(Snapshot.version_no.desc())
        .limit(1)
    ).first()


def _require_batch_size(raw_ids: Sequence[str]) -> None:
    """单次批量顺路取的单据数上限（`MAX_JOB_ORDERS_PER_BATCH`，design.md D2）。

    与 `allocate._require_batch_size` 同一原则：判在报文层 422、提示拆分、不截断 ——
    理由见那个函数（领域错误统一走 `errors.error_body`、截断会让调用方以为超出的也派生了）。
    """
    submitted = len(raw_ids)
    if submitted <= MAX_JOB_ORDERS_PER_BATCH:
        return
    raise ValidationBlocked(
        f"本批提交了 {submitted} 条单据，超过单次上限 {MAX_JOB_ORDERS_PER_BATCH} 单 ——"
        f"请拆分为每批不超过 {MAX_JOB_ORDERS_PER_BATCH} 单后分批提交；"
        "端点不截断到上限继续执行",
        detail={"submitted": submitted, "max": MAX_JOB_ORDERS_PER_BATCH},
    )


def _require_outbound(orders: Sequence[JobOrder]) -> None:
    """本批必须**全是出库单**，否则整批 422（design.md D2 第 1 条）。

    顺路取只对 `OUTBOUND` 有意义；混进一张入库/移库单说明调用方把队列选错了，整批拒绝
    而不是逐单跳过 —— 跳过会把「选错类型」记成一次「只有部分单生成了顺路取」的正常结果。
    """
    offenders = sorted(
        str(order.id) for order in orders if order.job_type is not JobType.OUTBOUND
    )
    if offenders:
        raise ValidationBlocked(
            f"本批有 {len(offenders)} 张单不是出库单：{offenders} —— 顺路取只处理 OUTBOUND",
            detail={"not_outbound": offenders, "expected": JobType.OUTBOUND.value},
        )


def _require_derivable(orders: Sequence[JobOrder]) -> None:
    """本批必须整批处于 `PENDING` 或 `PLANNED`，否则整体拒绝（design.md D3）。

    `PENDING` 首次派生、`PLANNED` 幂等命中/视图推进重新派生；其余状态（已确认 / 已驳回 /
    已执行等）不该再派生，整批 409 —— 与 `allocate._require_pending` 同一理由：跳过会
    把状态错记成「部分成功」。
    """
    offenders = sorted(
        (
            {"job_order_id": str(order.id), "status": order.status.value}
            for order in orders
            if order.status not in (JobStatus.PENDING, JobStatus.PLANNED)
        ),
        key=lambda item: int(item["job_order_id"]),
    )
    if offenders:
        listed = "、".join(f"{item['job_order_id']}（{item['status']}）" for item in offenders)
        raise StateConflict(
            f"本批有 {len(offenders)} 张单不处于 {JobStatus.PENDING.value}/{JobStatus.PLANNED.value}："
            f"{listed} —— 顺路取只派生待派生/已派生的单，整批拒绝",
            detail={"not_derivable": offenders},
        )


def _render_snapshot_version(snapshot: Snapshot) -> str:
    """`snapshot_version` = `snapshot_time` 的 `"%Y-%m-%dT%H:%M"` 渲染（D2，同 allocate D10）。"""
    return snapshot.snapshot_time.strftime("%Y-%m-%dT%H:%M")


def _current_pick_plan(session: Session, *, order: JobOrder) -> RecommendationPlan | None:
    """该单「当前方案」= `plan_kind=PICK` 且 `id` 最大的一行（D3；无则 `None`）。

    `RecommendationPlan.job_order_id` 不唯一（多次派生追加行），当前方案不加指针列，
    取 `id` 最大者 —— 与 `allocate` 对配置版本「取 `version_no` 最大」同一手法。
    """
    return session.scalars(
        sa.select(RecommendationPlan)
        .where(
            RecommendationPlan.job_order_id == order.id,
            RecommendationPlan.plan_kind == PlanKind.PICK,
        )
        .order_by(RecommendationPlan.id.desc())
        .limit(1)
    ).first()


def _require_locations(order: JobOrder, item: ConfirmItem) -> None:
    """按 `job_type` 校验库位字段的填法（`17` §4.3 台账矩阵）。

    入库只目标、出库「目标必空、源可空」（多巷无单一源库位，拣货路径走 `pick_path`，D4）、
    移库两者都有。**判在报文层**：DB 的 `_LEDGER_LOCATION_CHECK` 会把「填错哪一格」拦成
    `IntegrityError`（500），而这是调用方的报文错，应 422。`pick_path` 的缺失不在这里判 ——
    由确认编排读该单当前方案回退（D4）。
    """
    source = item.source_location_code
    target = item.target_location_code
    required = {
        JobType.INBOUND: ("target", source is None and target is not None),
        JobType.OUTBOUND: ("目标为空", target is None),
        JobType.RELOCATE: ("both", source is not None and target is not None),
    }[order.job_type]
    label, ok = required
    if ok:
        return
    raise ValidationBlocked(
        f"作业单 #{order.id}（{order.job_type.value}）的库位字段填法不合法 —— "
        f"入库只填 target、出库 target 必空（源可空、拣货路径走 pick_path）、移库两者都填"
        f"（当前 source={source!r}、target={target!r}）",
        detail={"job_order_id": str(order.id), "job_type": order.job_type.value},
    )


def _confirm_one(
    session: Session,
    *,
    order: JobOrder,
    item: ConfirmItem,
    operator_id: int,
    executed_at: datetime,
    snapshot: Snapshot | None,
) -> JobOrder:
    """按 `job_type` 分派到三类确认编排（`design.md` D5 的共用链）。"""
    if order.job_type is JobType.INBOUND:
        return confirm_inbound(
            session,
            job_order=order,
            operator_id=operator_id,
            executed_at=executed_at,
            target_location_code=item.target_location_code,
            actual_qty=item.actual_qty,
            snapshot=snapshot,
            lock_version=item.lock_version,
        )
    if order.job_type is JobType.OUTBOUND:
        return confirm_outbound(
            session,
            job_order=order,
            operator_id=operator_id,
            executed_at=executed_at,
            source_location_code=None,
            actual_qty=item.actual_qty,
            snapshot=snapshot,
            lock_version=item.lock_version,
            # pick_path 是 Pydantic `PickPathItem` 列表，台账要的是 JSON 形状（D4）。
            pick_path_json=(
                [entry.model_dump() for entry in item.pick_path]
                if item.pick_path is not None
                else None
            ),
        )
    return confirm_relocate(
        session,
        job_order=order,
        operator_id=operator_id,
        executed_at=executed_at,
        source_location_code=item.source_location_code,
        target_location_code=item.target_location_code,
        actual_qty=item.actual_qty,
        snapshot=snapshot,
        lock_version=item.lock_version,
    )


@router.post("/batch/confirm", response_model=BatchConfirmResponse)
def batch_confirm(
    payload: BatchConfirmRequest,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
) -> BatchConfirmResponse:
    """批量确认：逐单独立提交（`design.md` D7），个别单失败回 `PLANNED` 不影响同批其余单。

    步骤：

    1. **报文能当场判错的先全判掉**（形状 / 重号 / 不存在 / 库位填法）—— 与 `allocate`
       同一原则：「报文错」与「状态错」的先后不取决于哪一步先查库。这些是**整批**拒绝。
    2. **逐单确认**：每张单 `confirm_*` 之后**各自 `commit()`**。写台账失败的单由编排
       就地退回 `PLANNED`（不抛异常），其余单照常 `EXECUTED → VERIFIED`；源状态非
       `PLANNED` 的单抛 `StateConflict`，本端点**只记这一单**、继续处理同批其余单 ——
       这是 D7「逐单独立事务」与 `allocate`「整批拒绝」的差别所在。
    """
    now = datetime.now()

    # 1. 报文 → id → 作业单 → 库位填法，全是整批拒绝的报文级校验（见 docstring）。
    ids = _parse_ids([item.job_order_id for item in payload.orders])
    orders = _load_orders(session, warehouse_id=payload.warehouse_id, ids=ids)
    for item in payload.orders:
        _require_locations(orders[int(item.job_order_id)], item)

    snapshot = _current_snapshot_or_none(session, warehouse_id=payload.warehouse_id)

    # 2. 逐单确认 + 逐单提交。`now` 取一次（「同样输入必得同样输出」）；`operator_id` 取
    #    当前账号。失败面分两种：写台账失败（编排内回退 `PLANNED`，正常分支）与
    #    源状态非 `PLANNED`（`StateConflict`，本端点记录后继续）。
    results: list[ConfirmOutcome] = []
    for item in payload.orders:
        order = orders[int(item.job_order_id)]
        try:
            result = _confirm_one(
                session,
                order=order,
                item=item,
                operator_id=account.id,
                executed_at=now,
                snapshot=snapshot,
            )
        except StateConflict as exc:
            # 守卫在迁移之前抛，无写入；不 rollback（会作废已提交单的可见性判据），
            # 直接记录这一单并继续。`order.status` 读到的仍是库里的原状态。
            results.append(
                ConfirmOutcome(
                    job_order_id=item.job_order_id, status=order.status, error=exc.message
                )
            )
            continue
        # 逐单独立提交（D7）：这一单的台账 / cap 增量 / 后验是一个完整事务。
        session.commit()
        results.append(
            ConfirmOutcome(job_order_id=item.job_order_id, status=result.status)
        )

    return BatchConfirmResponse(results=results)


@router.post("/batch/pick-sequence", response_model=BatchPickSequenceResponse)
def batch_pick_sequence(
    payload: BatchPickSequenceRequest,
    session: Session = Depends(get_db),
    _authorized: None = Depends(require_permission(Permission.OUTBOUND_OPERATE)),
) -> BatchPickSequenceResponse:
    """批量顺路取派生（design.md D2 / D3）：对一组 `OUTBOUND` 单按巷道聚合既有库存，
    产出拣货顺序 + 加权集中度，写 `RecommendationPlan(plan_kind=PICK)` 并迁 `PLANNED`。

    **只读派生**：不调 `engine.invoke`、不重新决定落位、不写台账/库存（D1）。步骤与失败面：

    1. **报文 → id**：上限 / 形状 / 重号 / 不存在 / 非出库单 ⇒ 整批 422，零写入。
    2. **状态**：有单不在 `PENDING`/`PLANNED` ⇒ 整批 409（D3 只派生这两种源状态）。
    3. **快照**：无快照 ⇒ 409（`BlockedMissingPrerequisite`，提示重新导入，不猜测）。
    4. **逐单派生**（D3 幂等键 = `bulk_batch_no` × 库存视图版本）：
       - `PENDING`：`profile.plates_by_aisle` 空 → 分列 `not_in_stock`（货未入库，停留
         `PENDING`，不阻断）；否则派生、写方案、迁 `PLANNED`、回写 `bulk_batch_no`。
       - `PLANNED`：读「当前方案」（`id` 最大的 `PICK` 行）—— 同版本返既有（幂等命中，
         不重复写、不重迁）；异版本（或无方案）重新派生并**追加**一行新方案（`id` 更大，
         天然成为当前方案），不重迁状态。
    5. **整批一次提交**（与 `allocate` 同口径）：纯派生 + 写方案，无台账/库存写，不存在
       「一半有方案一半没有」的中间态。
    """
    now = datetime.now()

    # 1~3. 报文级校验 + 状态 + 快照，全是整批拒绝（见 docstring）。
    _require_batch_size(payload.job_order_ids)
    ids = _parse_ids(payload.job_order_ids)
    orders_by_id = _load_orders(session, warehouse_id=payload.warehouse_id, ids=ids)
    orders = [orders_by_id[value] for value in ids]
    _require_outbound(orders)
    _require_derivable(orders)

    snapshot = _current_snapshot_or_none(session, warehouse_id=payload.warehouse_id)
    if snapshot is None:
        raise BlockedMissingPrerequisite(
            f"仓库 {payload.warehouse_id} 没有任何快照基线 —— 阻断本次顺路取派生，不产出方案："
            "请先导入库存快照（顺路取按库存视图聚合，无快照即无分布可读）"
        )
    snapshot_version = _render_snapshot_version(snapshot)
    if payload.snapshot_version is not None and payload.snapshot_version != snapshot_version:
        raise StateConflict(
            f"声明的快照版本 {payload.snapshot_version} 不是本次实际使用的那一版"
            f"（{snapshot_version}，快照 #{snapshot.id}）—— 请重新读取当前快照后再提交",
            detail={"declared": payload.snapshot_version, "actual": snapshot_version},
        )

    index = load_snapshot_index(session, snapshot_id=snapshot.id)
    bulk_batch_no = next_bulk_batch_no(
        load_bulk_batch_nos(session, warehouse_id=payload.warehouse_id, now=now), now=now
    )

    # 4. 逐单派生（请求序）。纯派生 + 写方案，`now` 取一次；`bulk_batch_no` 只回写
    #    本批真正迁 `PLANNED` 的单。
    plans: list[PickPlanItem] = []
    not_in_stock: list[str] = []
    for order in orders:
        profile = index.profile(order.material_code)

        if order.status is JobStatus.PLANNED:
            current = _current_pick_plan(session, order=order)
            if current is not None and current.payload_json.get("snapshot_version") == snapshot_version:
                # 幂等命中：同版本返既有方案，不重复写、不重迁状态（D3）。
                plans.append(
                    PickPlanItem(
                        job_order_id=str(order.id),
                        plan_id=current.id,
                        **current.payload_json,
                    )
                )
                continue
            # 视图推进（或无方案）：追加一行新方案，不重迁状态（已 PLANNED）。
            result = derive_pick_sequence(
                material_code=order.material_code, do_no=order.order_no, profile=profile
            )
            payload_json = {**result, "snapshot_version": snapshot_version}
            row = RecommendationPlan(
                warehouse_id=order.warehouse_id,
                job_order_id=order.id,
                plan_kind=PlanKind.PICK,
                payload_json=payload_json,
            )
            session.add(row)
            session.flush()
            plans.append(
                PickPlanItem(job_order_id=str(order.id), plan_id=row.id, **payload_json)
            )
            continue

        # PENDING：货未入库分列 not_in_stock（停留 PENDING，不阻断）；否则派生 + 迁 PLANNED。
        if not profile.plates_by_aisle:
            not_in_stock.append(str(order.id))
            continue
        result = derive_pick_sequence(
            material_code=order.material_code, do_no=order.order_no, profile=profile
        )
        payload_json = {**result, "snapshot_version": snapshot_version}
        row = RecommendationPlan(
            warehouse_id=order.warehouse_id,
            job_order_id=order.id,
            plan_kind=PlanKind.PICK,
            payload_json=payload_json,
        )
        session.add(row)
        session.flush()
        order.bulk_batch_no = bulk_batch_no
        order.status = assert_transition(order.status, JobStatus.PLANNED)
        bump_lock_version(order, expected=order.lock_version)
        plans.append(
            PickPlanItem(job_order_id=str(order.id), plan_id=row.id, **payload_json)
        )

    # 5. 整批一次提交（与 `allocate` 同口径）。
    session.commit()

    return BatchPickSequenceResponse(
        bulk_batch_no=bulk_batch_no,
        snapshot_version=snapshot_version,
        plans=plans,
        not_in_stock=not_in_stock,
    )


@router.post("/{job_id}/reject", response_model=JobStatusResponse)
def reject_job(
    job_id: str,
    payload: RejectRequest,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
) -> JobStatusResponse:
    """驳回：`PLANNED → REJECTED`，写 `reject_reason`（`17` §4.5 / `15` §6.2）。

    **未确认不产生台账**（`CLAUDE.md` §四）：驳回只动状态与驳回原因，不写台账、不写 cap。
    非 `PLANNED` 的单驳回撞状态守卫（`StateConflict`，409）。
    """
    order = _load_order(session, warehouse_id=payload.warehouse_id, job_id=job_id)
    order.status = assert_transition(order.status, JobStatus.REJECTED)
    order.reject_reason = payload.reason
    session.commit()
    return JobStatusResponse(job_order_id=job_id, status=order.status)


@router.post("/{job_id}/retry", response_model=JobStatusResponse)
def retry_job(
    job_id: str,
    payload: RetryRequest,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
) -> JobStatusResponse:
    """后验重试：`VERIFY_FAILED → VERIFYING → VERIFIED / VERIFY_FAILED`（`15-01` §3.1）。

    重试**就是** `run_verification`（「无放弃后验终态」由「不存在别的后验入口」兑现）。
    快照取当前基线；缺失时**阻断**（`BlockedMissingPrerequisite`，409 —— 仍缺快照无从
    重算，提示重新导入，不静默 PASS 也不迁 `VERIFY_FAILED`）。
    """
    order = _load_order(session, warehouse_id=payload.warehouse_id, job_id=job_id)
    snapshot = _current_snapshot_or_none(session, warehouse_id=payload.warehouse_id)
    run_verification(session, job_order=order, snapshot=snapshot)
    session.commit()
    return JobStatusResponse(job_order_id=job_id, status=order.status)


@router.post("/{job_id}/void", response_model=JobStatusResponse)
def void_order(
    job_id: str,
    payload: VoidRequest,
    session: Session = Depends(get_db),
    account: Account = Depends(current_account),
) -> JobStatusResponse:
    """冲正：`EXECUTED / VERIFIED → VOID` + 反向台账行 + 反向 cap / 库存增量（同一事务）。

    编排在 `services/void.py`。非 `EXECUTED / VERIFIED` 源状态撞守卫（`StateConflict`，409）；
    `VOID` 后不可再冲正（终态，幂等）。
    """
    order = _load_order(session, warehouse_id=payload.warehouse_id, job_id=job_id)
    snapshot = _current_snapshot_or_none(session, warehouse_id=payload.warehouse_id)
    void_job(
        session,
        job_order=order,
        operator_id=account.id,
        voided_at=datetime.now(),
        snapshot=snapshot,
    )
    session.commit()
    return JobStatusResponse(job_order_id=job_id, status=order.status)


@reads_router.get("/ledger", response_model=list[LedgerItem])
def list_ledger(
    warehouse_id: str = Query(min_length=1, max_length=32),
    job_order_id: str = Query(min_length=1),
    session: Session = Depends(get_db),
) -> list[LedgerItem]:
    """按作业单查台账：正常行 + 反向行都返回（`is_reversal` 区分）。

    spec「台账查询返回反向行」：冲正后的单有两行（`[False, True]`），按 `id` 升序。
    """
    order = _load_order(session, warehouse_id=warehouse_id, job_id=job_order_id)
    rows = session.scalars(
        sa.select(Ledger).where(Ledger.job_order_id == order.id).order_by(Ledger.id)
    )
    return [LedgerItem.model_validate(row) for row in rows]


@reads_router.get("/verification/{job_id}", response_model=list[VerificationItem])
def list_verifications(
    job_id: str,
    warehouse_id: str = Query(min_length=1, max_length=32),
    session: Session = Depends(get_db),
) -> list[VerificationItem]:
    """查某作业单的后验结果：一行一条指标（入库两条、出库 / 移库一条，`17` §4.4）。"""
    order = _load_order(session, warehouse_id=warehouse_id, job_id=job_id)
    rows = session.scalars(
        sa.select(Verification)
        .where(Verification.job_order_id == order.id)
        .order_by(Verification.id)
    )
    return [VerificationItem.model_validate(row) for row in rows]


@reads_router.get("/deviation", response_model=list[DeviationItem])
def list_deviation(
    warehouse_id: str = Query(min_length=1, max_length=32),
    status: DeviationStatus | None = Query(default=None),
    session: Session = Depends(get_db),
) -> list[DeviationItem]:
    """列出本仓的偏离批次清单（移库任务来源，`17` §4.4）。`status` 可选过滤。

    spec「偏离批次标记」：后验超标写入的 `Deviation` 在这里可查，操作员据此发起收拢。
    聚合与分页属阶段六 KPI 看板，本端点只落「可查」（design.md D5）。
    """
    rows = kpi.list_deviations(session, warehouse_id=warehouse_id, status=status)
    return [DeviationItem.model_validate(row) for row in rows]


@reads_router.get("/jobs", response_model=list[JobQueueItem])
def list_jobs(
    warehouse_id: str = Query(min_length=1, max_length=32),
    type: JobType = Query(...),
    status: JobStatus | None = Query(default=None),
    material_code: str | None = Query(default=None),
    abc_class: AbcClass | None = Query(default=None),
    order_no: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> list[JobQueueItem]:
    """按类型查作业队列（openspec/changes/inbound-domain/design.md D1 / D3 / D4）。

    - `type` 必填且用 `JobType` 枚举 —— 非法取值在参数校验层即 422，避免「拼错类型
      返回空队列」被误读成「无数据」。端点类型无关（`WHERE job_type = :type` 一个通式），
      `outbound` / `relocate` 由 28-03 / 28-04 复用同一端点。
    - 可选 `status` / `material_code` / `abc_class` 为**精确**筛选，`order_no` 为**前缀**
      匹配（design.md D3：前端搜索框映射到 `order_no`）。
    - 纯 `SELECT`：不取业务钟、不 `commit`、不改 `lock_version`、不迁移状态
      （spec「查询不改变状态」）。跨仓隔离靠 `warehouse_id` 进查询（`CLAUDE.md` §七）。
    """
    stmt = sa.select(JobOrder).where(
        JobOrder.warehouse_id == warehouse_id,
        JobOrder.job_type == type,
    )
    if status is not None:
        stmt = stmt.where(JobOrder.status == status)
    if material_code is not None:
        stmt = stmt.where(JobOrder.material_code == material_code)
    if abc_class is not None:
        stmt = stmt.where(JobOrder.abc_class == abc_class)
    if order_no is not None:
        stmt = stmt.where(JobOrder.order_no.startswith(order_no))
    rows = session.scalars(stmt.order_by(JobOrder.id))
    return [JobQueueItem.model_validate(row) for row in rows]


@reads_router.get("/plan/{plan_id}", response_model=dict)
def read_plan_reason(
    plan_id: str,
    warehouse_id: str = Query(min_length=1, max_length=32),
    session: Session = Depends(get_db),
) -> dict:
    """按 `plan_id` 取推荐理由体（openspec/changes/inbound-domain/design.md D8）。

    - `plan_id` = `RecommendationPlan.id` 的 `str` 形态，走 `_ID_PATTERN` 校验。
    - `WHERE id == :pid AND warehouse_id == :wid`：**跨仓就是未知** —— 别的仓的方案在本
      接口里不返回、不泄露存在性（对齐 `_load_order` 的 `ValidationBlocked` 422）。
    - 透传 `payload_json`，不复制副本、不重新校验形状：理由体只有一个来源（库里的方案行，
      `allocate.py` D10「理由不随 `plans[]` 返回、按 `plan_id` 另取」），`response_model=dict`
      让三类方案（分配 / 顺路取 / 收拢，`17` §10.1~10.3）共用同一端点而不绑死某一种形状。
    """
    if not _ID_PATTERN.match(plan_id):
        raise ValidationBlocked(
            f"方案标识 {plan_id!r} 的形状不合法 —— 本接口用 str(RecommendationPlan.id)",
            detail={"plan_id": plan_id},
        )
    row = session.scalars(
        sa.select(RecommendationPlan).where(
            RecommendationPlan.id == int(plan_id),
            RecommendationPlan.warehouse_id == warehouse_id,
        )
    ).first()
    if row is None:
        raise ValidationBlocked(
            f"方案不存在或不属于本仓（{warehouse_id}）：{plan_id}",
            detail={"missing_plan_ids": [int(plan_id)]},
        )
    return row.payload_json
