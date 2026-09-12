"""批量分配端点 `POST /api/allocate/batch`（`17` §10.7 / `14` §3.4）。

事实来源：`17` §10.7（请求 / 响应 / 字段表）、§4.1（作业单标识）、§4.2（方案实体）
          `14` §3.4（批量竞争分配的六步）、§3.5（A 类降级告警）
          `design.md` D1（端点 = 组装点）、D3（配置缺席的两种处置）、D4（整批取数一次、
          扣减只落内存）、D8（三处降级不混用）、D9 第 3 条（列与 JSON 是投影）、
          D10（端点契约）、D11（批次号）、D12（事务边界）、D16（`aisles` 单条 /
          `snapshot_version` 的选型）、D17（业务钟）
          spec `recommendation-engine`「批量分配端点契约」
          `tasks.md` 8.1 / 8.2（本模块的任务书）

## 这里是什么、不是什么

**是组装点**：四个取数函数各读一段（权重 / 快照索引 / 巷道+cap / 站台），交给
`allocate_batch` 做决定，`predict_cross_aisles` 做整批回溯，最后逐单 `build_reason_payload`
组装理由并落库（D1 的调用链在这里收口）。

**不是一个评分模块**：端点自己不评一个分、不选一条巷道、不改一个 `degraded`。这条边界
不是洁癖 —— 红线「同样输入必得同样输出」的可审性依赖它：引擎是纯函数（`now` / 权重 /
状态全部注入），端点只碰会话与报文，于是「同一批输入为什么出这个结果」可以只用引擎的
入参复算一遍，不必重放 HTTP。

## 8.2 的状态迁移与批次号回写（以及本模块此刻**不**做的事）

**回写的判据是「这一单出了方案」**：`batch.outcomes` 里的单领 `bulk_batch_no`、迁
`PENDING → PLANNED`、`lock_version` 推进一格；四级走尽、只出现在 `batch.failures` 里的单
**停留 `PENDING`**（D12）—— 失败的处置不是本端点的取舍，任务书把状态搬到了 8.2。

**非 `PENDING` 的单是整批拒绝**（`_require_pending`），不是逐单跳过；请求里没有版本号，
「同一批不会被分配两遍」由**状态守卫**兑现（任务书 8.2 的原话：乐观锁语义经状态守卫兑现）。
`lock_version` 照样推进：它保的不是这次调用（没有携带版本的人），而是**下一个**带着版本
来的调用方（二次确认卡）——行变了而版本没变，那边会以为自己读到的还是旧内容。

**本模块不碰**：台账与 cap（8.3 的验证条件 —— 它本来就没碰，不是本模块的取舍）、
规模上限与空数组（8.5）。权限与认证（8.4）不是「不碰」：401 由中间件全局覆盖，
第 2 层 403 由 `require_permission(inbound.operate)` 施加（仅仓管员/管理员，`13` §2.2）。

## 三处口径由本模块定，理由写在这里（`design.md` D10 的补记同步登记）

1. **作业单标识的线上形态 = `str(JobOrder.id)`**（十进制正整数、无前导零，按 `_ID_PATTERN`
   严格校验）。放宽写法不会报错，只会让同一行以 `"7"` 与 `"007"` 两种形态进队，而
   `order_queue` 是按单据号索引的 —— 它被扣两次容量、在响应里出现两次，且
   `build_priorities` 的重号检查（按字符串判重）看不见这件事。
2. **当前快照 = 本仓 `version_no` 最大者**。`Snapshot` 没有 `effective_at`（它是数据版本，
   不是配置版本），故不复用 `config_version.pick_current_version`；`version_no` 的列注释即
   「按仓库单调递增」（`17` §3.2）。
3. **`outbound_qty` 恒为 `None`**（阶段三没有交货单的取数来源，见 `_as_item`）：它走的是
   「排序降级」分支，理由里会写明，而不是塞一个 0 把「没有数据」伪装成「值为零」。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, time
from typing import NamedTuple

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.api.permissions import Permission
from app.core.concurrency import bump_lock_version
from app.core.config import settings
from app.core.config_version import pick_current_version
from app.core.enums import JobStatus
from app.core.errors import BlockedMissingPrerequisite, StateConflict, ValidationBlocked
from app.core.state_machine import assert_transition
from app.engine.allocator import AllocationItem, allocate_batch, predict_cross_aisles
from app.engine.factors import load_snapshot_index, load_station_weights
from app.engine.reasons import build_reason_payload, load_bulk_batch_nos, next_bulk_batch_no
from app.engine.scoring import load_aisle_state, load_weights
from app.models.configuration import CapacityConfig
from app.models.job import JobOrder, PlanKind, RecommendationPlan
from app.models.linkage import Snapshot
from app.models.master_data import Aisle, Material
from app.schemas.reason import (
    MAX_JOB_ORDERS_PER_BATCH,
    BatchAllocateRequest,
    BatchAllocateResponse,
    PlanItem,
)

router = APIRouter(prefix="/api/allocate")

#: 作业单标识的线上形态：`str(JobOrder.id)` —— 十进制正整数、无前导零、无符号。
#: 收紧到「恰好一种写法」的理由见模块 docstring 第 1 条。
_ID_PATTERN = re.compile(r"^[1-9]\d*$")


class CapacitySettings(NamedTuple):
    """本批要用的容量口径（`CapacityConfig` 缺席时取引导值，D3）。"""

    #: 近站台预留池的释放时刻（`17` §七 的 `reserved_release_at`）。
    release_at: time
    #: 同物料跨巷道阈值（`18` §1.3 的验收指标在分配时的预演口径）。
    same_material_cross_aisle_threshold: int


def _site_now() -> datetime:
    """业务钟：**现场墙上时间**（朴素，D17），整批只取一次（D2 第 3 条）。

    `app/core/clock.py` 的 `datetime.now().astimezone().replace(tzinfo=None)` 那一步是
    「带时区的值 → 现场墙上时间」的**换算**；这里没有可换算的对象 —— `datetime.now()`
    取的已经是现场墙上时间，故不套那一步（`require_wall_clock` 只拒带时区的值，它认可的
    正是这个形态）。

    **本端点是全链路唯一的取钟点**：批次号的现场日期（D11）、预留池是否已释放（D7）、
    理由里的时刻，都读这一个值。循环里逐单取钟会让同一批的单按不同钟点判「释放了没有」，
    「同样输入必得同样输出」当场不成立。
    """
    return datetime.now()


def _require_batch_size(raw_ids: Sequence[str]) -> None:
    """单次批量分配的单据数上限（`design.md` D10 / spec「规模上限与评分性能」，8.5）。

    **判在报文层**：超限是调用方的报文错误，与「未知单号」「形状不合法」同类，故在这里
    拦成 422 —— 而**不**写成 `job_order_ids` 上的 `Field(max_length=...)`。两个理由，都要
    说清，否则下一版很容易「顺手加上那个更短的写法」：

    - Pydantic 层的 422 是**另一种响应体**（FastAPI 默认的 `{"detail": [...]}`，没有
      `error` / `message`）。本端点把领域错误的形状统一在 `errors.error_body` 上，前端
      拦截器按 `error` 分流 —— 让一条上限错误走另一种形状，前端就得分两个分支。
    - 文档要的是「拒绝并**提示拆分**」：提示语里得有上限数与下一步动作（拆开分批提交），
      那是只有这里给得出的。Pydantic 的通用文案不会叫调用方去拆分。

    **判在原始条数上**，不是去重后的条数：`_requested_ids` 会去重，若先去重再判长度，
    一份 60 条里带 10 条重号的请求会以「实为 50 条」被放行 —— 那依然是一次**静默削减**，
    正是本条要禁掉的那件事。原始条数才是调用方实际提交的规模（规格的措辞也是「请求提交了
    51 条单据号」）。
    """
    submitted = len(raw_ids)
    if submitted <= MAX_JOB_ORDERS_PER_BATCH:
        return
    raise ValidationBlocked(
        f"本批提交了 {submitted} 条单据，超过单次上限 {MAX_JOB_ORDERS_PER_BATCH} 单 ——"
        f"请拆分为每批不超过 {MAX_JOB_ORDERS_PER_BATCH} 单后分批提交；"
        "端点不截断到上限继续执行（静默截断会让调用方以为超出的部分也分配了）",
        detail={"submitted": submitted, "max": MAX_JOB_ORDERS_PER_BATCH},
    )


def _requested_ids(raw_ids: Sequence[str]) -> tuple[int, ...]:
    """报文里的单号 → 整数 id；「写法不合法」与「重号」在这里拦成 422。

    两者都是**报文错误**（调用方改报文就能过），故走 `ValidationBlocked` 而不是让引擎
    抛 `ValueError`：`build_priorities` 的重号检查在引擎内，一个裸 `ValueError` 出来就是
    500 —— 而 500 说的是「服务端故障」，指错了方向，也会让调用方失去重试的判断依据。
    """
    ids: list[int] = []
    for raw in raw_ids:
        if not _ID_PATTERN.match(raw):
            raise ValidationBlocked(
                f"作业单标识 {raw!r} 的形状不合法 —— 本接口用 str(JobOrder.id)"
                "（十进制正整数、无前导零）：同一行有两种写法会让它被当成两条队项",
                detail={"job_order_id": raw},
            )
        ids.append(int(raw))

    duplicated = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicated:
        raise ValidationBlocked(
            f"作业单标识重复 {duplicated} —— 同一张单在同一批里只能出现一次",
            detail={"duplicated_job_order_ids": duplicated},
        )
    return tuple(ids)


def _load_orders(
    session: Session, *, warehouse_id: str, ids: Sequence[int]
) -> tuple[JobOrder, ...]:
    """按 id 取本仓的作业单；少一条即**整批**拒绝。

    「少一条」不放行、不跳过：跳过的表现是「响应里少一张单」而状态码仍是 200，
    调用方会以为它只是没排上 —— 把一次传参错误记成一次正常的分配结果。

    `warehouse_id` 必须进查询（`CLAUDE.md` §七 的数据隔离）：别的仓的单在本接口里就是
    **未知单号**。漏掉这个条件不会报错，它会一路走完并写进本仓的方案表。

    返回序 = 请求序。下游（`priority.order_queue`）按 `(priority 降序, job_order_id 升序)`
    重排，故取出序不影响结果 —— 但不影响不等于可以随便给：显式地沿请求序返回，
    是为了让「这里没有隐式的次序依赖」可读（D2 第 2 条要的是遍历序显式给出）。
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
    return tuple(orders[value] for value in ids)


def _require_pending(orders: Sequence[JobOrder]) -> None:
    """本批必须**整批**处于 `PENDING`，否则整体拒绝（D10 / D12）。

    **为什么是整批，而不是跳过非 `PENDING` 的单**：跳过会产出「提交 N 张单、得到 M 张方案、
    响应里没有任何提示」—— 调用方把一次状态错记成一次正常分配。D10 的原话是「其余状态
    **整体拒绝**（不部分成功）」。

    **为什么在这里判（进引擎之前）**：状态是**已读到的行的属性**，不必等引擎跑完才知道；
    早判一句，也让「整批拒绝」不依赖「引擎恰好没产出方案」这个巧合。8.1 那三条报文级拒绝
    （未知 / 重号 / 非规范形式）用的是同一条原则。

    报**全部**越界的单而不是第一张：调用方要一次把集合改对，而「一次报一张」会让他改一轮
    再撞一轮。按 id 升序（数值序，不是串的字典序）给出，免得同一批两次调用报出不同顺序。
    """
    offenders = sorted(
        (
            {"job_order_id": str(order.id), "status": order.status.value}
            for order in orders
            if order.status is not JobStatus.PENDING
        ),
        key=lambda item: int(item["job_order_id"]),
    )
    if offenders:
        listed = "、".join(f"{item['job_order_id']}（{item['status']}）" for item in offenders)
        raise StateConflict(
            f"本批有 {len(offenders)} 张单不处于 {JobStatus.PENDING.value}：{listed} ——"
            "批量分配只处理待分配的单，整批拒绝、不做部分成功",
            detail={"not_pending": offenders, "expected": JobStatus.PENDING.value},
        )


def _current_snapshot(session: Session, *, warehouse_id: str) -> Snapshot:
    """本仓当前使用的快照基线：`version_no` 最大者（模块 docstring 第 2 条）。

    **一版都没有 ⇒ 阻断整批**，两条理由（都不是「阻断更省事」）：

    - 响应契约的 `snapshot_version` 是**必填**且形状固定（D10 / `17` §10.7），它的值
      只能来自某一版快照的 `snapshot_time`。没有行就没有值，而端点自己编一个时点会比
      任何错误更糟 —— 调用方会拿它去对账。
    - 同一时点下 `AisleCap` 也无从读起（cap 行按快照版本冻结），于是四级降级链会在
      **每一张单**上走空。一次「数据没到位」被记成 N 次「单子分不出去」，指向的是引擎
      排不出来（`scoring.load_aisle_state` 的 docstring 记的是同一条理由）。

    与 9.5 的关系：9.5 要求「入库在**无快照**时按因子级降级、**不阻断**」，那条落在
    **引擎**（`allocate_batch` 吃 `SnapshotIndex.absent` 时三个库存类因子降级）——
    与这里的端点级关口并存，不是二选一。两处的登记见 `design.md`。
    """
    snapshot = session.scalars(
        sa.select(Snapshot)
        .where(Snapshot.warehouse_id == warehouse_id)
        .order_by(Snapshot.version_no.desc())
        .limit(1)
    ).first()
    if snapshot is None:
        raise BlockedMissingPrerequisite(
            f"仓库 {warehouse_id} 没有任何快照基线 —— 阻断本次分配，不产出方案："
            "请先导入库存快照（响应里的 snapshot_version 取自快照时点，无快照即无值可报）"
        )
    return snapshot


def _render_snapshot_version(snapshot: Snapshot) -> str:
    """`snapshot_version` = `snapshot_time` 的 `"%Y-%m-%dT%H:%M"` 渲染（D10）。

    **不是 `version_no`**：那是存储侧的版本号（`Snapshot.version_no` 的列注释明写
    「不放 17 §10.5 的时间戳串」），把它接进报文会让「同一时点、不同仓不同号」的号段
    冒充成时点，而两者的形状都是 `2026-09-08T00:00` 这类串，错了看不出来。
    """
    return snapshot.snapshot_time.strftime("%Y-%m-%dT%H:%M")


def _load_capacity_settings(
    session: Session, *, warehouse_id: str, now: datetime
) -> CapacitySettings:
    """取本批要用的容量口径；**没有生效的配置行时退回引导值**（D3 的「容量」行）。

    与权重缺席（`scoring.load_weights` 抛 `BlockedMissingPrerequisite`）是两种处置，
    差别在「有没有权威值可用」：权重的默认值是没人批准过的口径（阻断），而容量的六项在
    `16` §353~356 有逐项默认，且这些默认值正是 `CapacityConfig` 首次落库时的种子
    （`app/core/config.py` 的模块 docstring）。

    退回**在这里**而不是引擎里：引擎刻意不认 `Settings`（见 `predict_cross_aisles` 的
    docstring —— 它只认传进来的阈值），配置的缺席处置属于组装点的判断。
    """
    row = pick_current_version(
        session.scalars(
            sa.select(CapacityConfig).where(CapacityConfig.warehouse_id == warehouse_id)
        ),
        now=now,
    )
    if row is None:
        return CapacitySettings(
            release_at=time.fromisoformat(settings.reserve_release_at),
            same_material_cross_aisle_threshold=settings.same_material_cross_aisle_max,
        )
    return CapacitySettings(
        release_at=row.reserved_release_at,
        same_material_cross_aisle_threshold=row.same_material_cross_aisle_threshold,
    )


def _near_station_flags(session: Session, *, warehouse_id: str) -> dict[str, bool | None]:
    """`aisle_no` → `is_near_station` 的**三态**（`None` = 未导出，`17` §2.1）。

    整张表读、不按本批候选筛：候选集由引擎按 cap 现算，端点在这里先筛一遍会把
    「哪条巷道算近站台」变成两处口径。巷道主数据是小表（`17` §2.1：两位巷道号）。
    """
    return {
        aisle_no: is_near_station
        for aisle_no, is_near_station in session.execute(
            sa.select(Aisle.aisle_no, Aisle.is_near_station).where(
                Aisle.warehouse_id == warehouse_id
            )
        )
    }


def _load_materials(
    session: Session, *, warehouse_id: str, codes: Iterable[str]
) -> dict[str, Material]:
    """本批用到的料号 → 物料主数据行。

    可能**少**（成品清单未导入时一张都没有）：那不是错误，物料的 ABC 与板载量走各自的
    「缺失即降级」分支（`abc_factor` 的物料侧兜底、`to_occupied_cells` 的本阶段恒等）。
    故这里是 `dict.get` 语义，不抛错。
    """
    return {
        material.material_code: material
        for material in session.scalars(
            sa.select(Material).where(
                Material.warehouse_id == warehouse_id,
                Material.material_code.in_(codes),
            )
        )
    }


def _as_item(order: JobOrder, *, material: Material | None) -> AllocationItem:
    """作业单（+ 物料主数据）→ 引擎的队列项。

    **`outbound_qty` 恒为 `None`**：未来 N 天交货单出库量在阶段三**没有取数来源**
    （交货单属阶段四的导入管线）。传 `None` 走的是 4.2 的**排序降级**分支 —— 整批退化为
    仅按 ABC 排序，并逐单在 `priority.degrade_reason` 里写明。传一个 `0` 会让「没有这份
    数据」伪装成「数据在、值为零」：两者在理由里长得一样，而后者是「这个料近期确实不出货」，
    含义相反。
    """
    return AllocationItem(
        job_order_id=str(order.id),
        material_code=order.material_code,
        qty=order.qty,
        order_abc_class=order.abc_class,
        material_abc_class=material.abc_class if material is not None else None,
        outbound_qty=None,
        order_batch_no=order.batch_no,
        cartons_per_pallet=material.cartons_per_pallet if material is not None else None,
    )


def _as_plan_item(
    row: RecommendationPlan,
    payload,
    *,
    order: JobOrder,
    material: Material | None,
) -> PlanItem:
    """落库的那一行 + 理由体 → 响应里的一条方案（`17` §10.7 的 `plans[]`）。

    字段从**两处**取，边界是「谁是这个事实的来源」：

    - 单号 / 料号 / 品名 / ABC / 数量 = **作业单**（§10.7 的字段表把 `plans[]` 定义成
      当前队列项的视图）。`material_name` 沿 ABC 的同一方向兜底（单据侧优先、主数据侧
      兜底，`16` A.4），故物料的品名少一份也不会让这一栏空着。
    - 巷道 / 跨巷道 / 优先级 = **理由体**（`payload`）。它才是**落库**的那份陈述，响应
      与库里的方案因此逐字一致 —— 若这里改用 `outcome` 的字段，两者就成了同一次调用里
      抄写的两个副本，将来任何一处改动都能让它们分叉（D9 第 3 条的同一条论证）。

    理由体本身**不进响应**：按 `plan_id` 另取（D10 —— 同一份理由有两个副本，就有两个
    可能分叉的副本）。
    """
    return PlanItem(
        job_order_id=payload.job_id,
        order_no=order.order_no,
        material_code=order.material_code,
        material_name=order.material_name or (material.material_name if material else None),
        abc_class=order.abc_class,
        qty=order.qty,
        aisles=list(payload.aisles),
        predicted_cross_aisle=payload.predicted_cross_aisle,
        priority=payload.priority.score,
        plan_id=row.id,
    )


@router.post("/batch", response_model=BatchAllocateResponse)
def allocate_batch_plans(
    payload: BatchAllocateRequest,
    session: Session = Depends(get_db),
    _authorized: None = Depends(require_permission(Permission.INBOUND_OPERATE)),
) -> BatchAllocateResponse:
    """一次批量分配：定序 → 贪心选道 → 四级降级 → 组装理由并落库（`14` §3.4）。

    步骤与失败面（`design.md` D12 的失败/恢复表的端点侧）：

    1. **报文 → 可查的 id**：条数超过 50 / 形状不合法 / 重号 / 不存在 ⇒ 422，零写入。
       条数超限**拒绝并提示拆分、不截断**（8.5）—— 截断会让调用方以为超出的那些也分配了
    2. **状态**：有单不在 `PENDING` ⇒ **整批** 409，零写入（8.2 / D10 的「不部分成功」）
    3. **快照**：解析本次用哪一版；与调用方的声明不符 ⇒ 409，零写入；一版都没有 ⇒ 409
    4. **取数**（整批各一次，D4）：权重缺席 ⇒ 409（`load_weights` 抛）；容量配置缺席 ⇒
       退回引导值（D3）
    5. **决定**：`allocate_batch` 在内存里跑完，扣减**不落库**（D4 —— `AisleCap` 是这一版
       快照的冻结值，消费它的是阶段四的 cap 增量事务）
    6. **落库 + 回写**：逐单写 `RecommendationPlan`（列与 JSON 取同一次组装的结果），
       并给**出了方案的单**回写批次号、迁 `PLANNED`（8.2），**整批一次提交** ——
       「一半有方案一半没有」的中间态因此不存在（D12）

    分配失败的单（四级走尽）**不出现在 `plans` 里**，也不阻断同批其余单：`17` §10.7 的
    响应没有承载失败的字段，这是 D19 登记的未定项，端点侧能表达的只有「它不在 `plans` 里」——
    它同时**不被回写**（停留 `PENDING`、无批次号，D12），故「在 `plans` 里」与「被回写了」
    是同一件事的两种读法，不会分叉。
    """
    now = _site_now()

    # 1. 报文能当场判错的先全判掉，再去碰数据 —— 「报文错」与「数据没到位」两类 4xx
    #    的先后由此确定，而不是取决于哪一步先查库。组内自上而下 = 「整体的判决先于逐条的判决」：
    #    上限问的是「这一批能不能受理」，形状与重号问的是「里面哪一条不对」。
    _require_batch_size(payload.job_order_ids)
    ids = _requested_ids(payload.job_order_ids)
    orders = _load_orders(session, warehouse_id=payload.warehouse_id, ids=ids)

    # 1.5 状态：只要有一张单不是 PENDING 就整批拒绝（8.2 / D10）—— 与上面同一原则，
    #     而且同样在**碰引擎之前**判：整批拒绝不该依赖「引擎恰好没出方案」。
    _require_pending(orders)

    # 2. 本批用哪一版快照，以及调用方的声明是否与之一致（D16：声明不是筛选器）
    snapshot = _current_snapshot(session, warehouse_id=payload.warehouse_id)
    snapshot_version = _render_snapshot_version(snapshot)
    if payload.snapshot_version is not None and payload.snapshot_version != snapshot_version:
        raise StateConflict(
            f"声明的快照版本 {payload.snapshot_version} 不是本次实际使用的那一版"
            f"（{snapshot_version}，快照 #{snapshot.id} version_no={snapshot.version_no}）——"
            "请重新读取当前快照后再提交",
            detail={"declared": payload.snapshot_version, "actual": snapshot_version},
        )

    # 3. 取数：四处各一次，全是只读（D4）
    capacity = _load_capacity_settings(session, warehouse_id=payload.warehouse_id, now=now)
    weights = load_weights(session, warehouse_id=payload.warehouse_id, now=now)
    index = load_snapshot_index(session, snapshot_id=snapshot.id)
    state = load_aisle_state(
        session, warehouse_id=payload.warehouse_id, snapshot_id=snapshot.id
    )
    station_weights = load_station_weights(session, warehouse_id=payload.warehouse_id)
    is_near_station = _near_station_flags(session, warehouse_id=payload.warehouse_id)
    materials = _load_materials(
        session,
        warehouse_id=payload.warehouse_id,
        codes={order.material_code for order in orders},
    )

    # 批次号在**写入之前**取（D11 的计数口径 = 当日**已有**的号；8.2 回写后，同一事务里
    # 的第二次提交读到的是已回写的行 —— 计数与回写必须同事务，这里先把口径摆正）
    bulk_batch_no = next_bulk_batch_no(
        load_bulk_batch_nos(session, warehouse_id=payload.warehouse_id, now=now), now=now
    )

    # 4. 决定（纯函数，不碰会话）
    batch = allocate_batch(
        items=[
            _as_item(order, material=materials.get(order.material_code)) for order in orders
        ],
        weights=weights,
        state=state,
        snapshot=index,
        station_weights=station_weights,
        is_near_station=is_near_station,
        release_at=capacity.release_at,
        now=now,
    )
    predictions = predict_cross_aisles(
        batch, snapshot=index, threshold=capacity.same_material_cross_aisle_threshold
    )

    # 5. 组装理由 + 落库。按单号索引而不是按下标配对：`outcomes` 是队列序
    #    （`priority` 降序），与 `orders` 的请求序**不是同一个序**，按下标配会错位到
    #    「方案写对了、单写错了」这种不报错的形态。
    orders_by_id = {str(order.id): order for order in orders}
    plans: list[PlanItem] = []
    for outcome in batch.outcomes:
        order = orders_by_id[outcome.item.job_order_id]
        reason = build_reason_payload(
            outcome,
            weights=weights,
            predicted_cross_aisle=predictions[outcome.item.material_code],
        )
        row = RecommendationPlan(
            warehouse_id=order.warehouse_id,
            job_order_id=order.id,
            plan_kind=PlanKind.ASSIGN,
            payload_json=reason.model_dump(),
            # 列 = JSON 同名字段的**投影**，同一次写入（D9 第 3 条 / 17 §4.2）
            degraded=reason.degraded,
            degrade_reason=reason.degrade_reason,
        )
        session.add(row)
        # 取 `plan_id`：响应里的那份方案要指回这一行（§10.7 的 `plans[].plan_id`）
        session.flush()

        # 6. 回写（8.2）：**出了方案的单**才算成功者 —— 领批次号、迁 `PLANNED`。
        #    判据是「它在这份 `outcomes` 里」：四级走尽的单只出现在 `batch.failures`，
        #    故它们**停留 `PENDING`、不领批次号**（D12）—— 失败单若也写上批次号，
        #    「这单属于 BAT-xxx 批」在追溯时就是一句假话，而它同时不再是可重试的待分配单。
        order.bulk_batch_no = bulk_batch_no
        #    迁移走共用状态机而非直接赋值：`_require_pending` 已经整批把过一道，这里的
        #    守卫在今天是「必然通过」的 —— 但它是**另一件事**的保证：将来若有人放宽那道
        #    预检（比如改成「跳过非 PENDING」），非法迁移会在写入处当场报错，而不是静悄悄
        #    写出一行 `CANCELLED → PLANNED`（`state_machine` 明文列出「不新增未定义迁移」）。
        order.status = assert_transition(order.status, JobStatus.PLANNED)
        #    版本推进：行改了，版本就得跟着走（D1）。`expected` 取的是**本事务内**读到的值 ——
        #    本契约的请求里没有版本号（D10 的请求体只有三项，「重复提交必被拒」由状态守卫
        #    兑现，不是由版本比对），故这里的比对必然通过；调它要的是那个**唯一一处定义**
        #    的增量语义，而不是在校验一件事。
        bump_lock_version(order, expected=order.lock_version)
        plans.append(
            _as_plan_item(
                row,
                reason,
                order=order,
                material=materials.get(order.material_code),
            )
        )

    # 事务边界属于端点（`deps.get_db` 只回滚、不提交）：整批一次提交，
    # 「一半有方案、一半没有」的中间态由此不存在（D12）。
    session.commit()

    return BatchAllocateResponse(
        bulk_batch_no=bulk_batch_no,
        snapshot_version=snapshot_version,
        plans=plans,
        degraded_alerts=list(batch.alerts),
    )
