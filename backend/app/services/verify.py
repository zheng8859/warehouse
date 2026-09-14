"""后验三口径：**纯函数判定**（只算只判）+ **后验编排**（推进状态、落 `Verification`）。

事实来源：`15-00` §六（阈值表）/ §1.3（术语：逐单口径 vs 聚合口径）
          `15-01` §8.1（后验口径引用，三类作业的指标与不达标处理）
          `15-02` §8 / `15-03` §8.1 / `15-04` §8.1（三类口径差异）
          `17` §4.4（`Verification` / `Deviation` 实体）
          spec `transaction-base`「同步后验与三口径判定」
          `design.md` D5（verify = 三口径纯函数 + `PASS / DEVIATION` 判定 + 达标/偏离分派）

## 两层：纯函数算，编排落

**纯函数**（`verify_inbound` / `verify_outbound` / `verify_relocate` /
`concentration_aisle_count`）：一个函数得一个或多个 `MetricResult`（`metric_kind` /
`actual_value` / `threshold_value` / `verify_result`），**不碰会话、不写库**（`design.md`
D5「同样输入必得同样输出」）。

**编排**（`run_verification`）：会话内推进 `EXECUTED → VERIFYING → VERIFIED / VERIFY_FAILED`
并落 `Verification` 行，`VERIFYING` 不对外停留（`15-01` §3.3.2）；`VERIFY_FAILED → VERIFYING`
重试即**再次调用本编排**（无放弃后验终态）。`Deviation` 写入见 tasks 4.3，接在
`_write_verifications` 之后同一 savepoint —— 达标 / 偏离的 `Verification` 行都落，偏离的
指标再各落一条 `Deviation`（`PASS` 不落）。

## 三口径的输入为何不统一（各函数各自取什么）

1. **入库**取 `SnapshotIndex`：入库后验基于「**入库后**的库存分布」（`15-03` §8.1 的时序说明
   与入库后验相反），落位后该物料/该批在库里跨几条巷道，正是快照的 `profile` 要回答的问题。
   于是 `verify_inbound` 直接吃快照，自己算「同物料跨巷道」与「同批跨巷道」两个数。
2. **出库**取 `pick_qty_by_aisle`（巷道 → 拣货量）：加权集中度是「对一张 DO 按拣货量降序
   累加至 80% 所覆盖的巷道数」，数据来源 = 交货单行项目 × 库存视图（`15-03` §8.1），
   是**拣货分布**而不是整份库存分布 —— 喂整份快照反而让函数自己猜「哪个巷道的几板该被拣」。
   拣货分布由调用侧（确认编排 / 顺路取）给，函数只管「80% 落到几条巷道」这一件事。
3. **移库**取 `cross_aisle_before` / `cross_aisle_after` 两个整数：移库是**相对阈值**
   （`15-04` §8.1：比移库前更集中），比对「收拢前 vs 收拢后」的同物料跨巷道数。前值在
   台账写入前的快照上算、后值在台账写入后的快照上算 —— 两个数都由编排在**两侧各自**
   取好传入，纯函数只做「后值 < 前值 ?」这一个判断。

## 快照缺失时怎么办

`verify_inbound` 在 `snapshot.snapshot_present=False` 时**抛 `BlockedMissingPrerequisite`**
而不是静默 PASS：`SnapshotIndex.profile()` 对缺失快照返回空档案（`cross_aisle_count=0`），
若照常判定会得一个「0 ≤ 5 → PASS」的假达标 —— 与「快照缺失或过期 → 阻断」这条红线
（`CLAUDE.md` §四）同源。缺失不是「算出来达标」，是「没算」。快照缺失**直接阻断**
（409，不迁 `VERIFY_FAILED`）；「算不出来」的数据自相矛盾（如台账缺源库位）才落
`VERIFY_FAILED` 待重试 —— 两者分开，见 `run_verification` 的两段 except。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.enums import JobStatus, JobType, VerifyResult
from app.core.errors import BlockedMissingPrerequisite, DomainError, ValidationBlocked
from app.core.state_machine import assert_transition
from app.engine.factors import SnapshotIndex, aisle_of, load_snapshot_index
from app.models.job import Deviation, DeviationCauseKind, JobOrder, Ledger, Verification
from app.models.linkage import Snapshot

__all__ = [
    "MetricResult",
    "concentration_aisle_count",
    "run_verification",
    "verify_inbound",
    "verify_outbound",
    "verify_relocate",
]

#: 三口径的指标名（`15` §7.1 / `15-00` §1.3 的口径名，`Verification.metric_kind` 直接存它）。
#: 入库两条、出库 / 移库各一条 —— 一行一条指标（`17` §4.4）。
METRIC_MATERIAL_CROSS_AISLE = "同物料跨巷道"
METRIC_BATCH_CROSS_AISLE = "同批跨巷道"
METRIC_CONCENTRATION = "拣货量加权集中度"
METRIC_RELOCATE_DROP = "同物料跨巷道是否下降"

#: 入库后验主 / 辅口径阈值（`15-00` §六：同物料 ≤5、同批 ≤3）。
DEFAULT_MATERIAL_CROSS_AISLE = 5
DEFAULT_BATCH_CROSS_AISLE = 3

#: 出库加权集中度 N（`15-00` §六：默认 5，可配）。
DEFAULT_CONCENTRATION_N = 5


@dataclass(frozen=True)
class MetricResult:
    """**一条指标**的后验结果：实测值、阈值、`PASS / DEVIATION`。

    `actual_value` / `threshold_value` 用 `float` 对齐 `Verification` 的两列（`17` §4.4：
    实测值可能是 4.8 这种小数）。阈值随结果一并给出 —— 阈值可配（`15` §10.6），
    只存实测值的话，配置一改历史结果就被重新解读，与「历史台账不许改写」同一道理。
    """

    metric_kind: str
    actual_value: float
    threshold_value: float
    verify_result: VerifyResult

    @classmethod
    def judged(
        cls,
        metric_kind: str,
        *,
        actual: float | int,
        threshold: float | int,
        passes: bool,
    ) -> MetricResult:
        """按一个布尔判定构造结果。`passes` 由各口径自己算 —— 入库是 `≤`、移库是 `<`，
        比较符各不相同，收在这里会把「怎么比」和「比什么」捆在一起。"""
        return cls(
            metric_kind=metric_kind,
            actual_value=float(actual),
            threshold_value=float(threshold),
            verify_result=VerifyResult.PASS if passes else VerifyResult.DEVIATION,
        )

    @property
    def is_deviation(self) -> bool:
        return self.verify_result is VerifyResult.DEVIATION


def _batch_aisle_count(
    snapshot: SnapshotIndex, material_code: str, batch_no: str
) -> int:
    """同批跨巷道数 = 该批号在**该物料**名下出现的巷道数。

    读 `batches_by_material[material][aisle] = frozenset[batch_no]`：一个巷道一个批号集，
    数有几个巷道的集里含这个批号。与 `cross_aisle_count`（数板数 > 0 的巷道）是两种事实，
    故各自成对 —— 前者比「这个批在几条巷」，后者比「这个料在几条巷」。
    """
    aisles = snapshot.batches_by_material.get(material_code, {})
    return sum(1 for batches in aisles.values() if batch_no in batches)


def verify_inbound(
    *,
    material_code: str,
    batch_no: str,
    snapshot: SnapshotIndex,
    material_threshold: int = DEFAULT_MATERIAL_CROSS_AISLE,
    batch_threshold: int = DEFAULT_BATCH_CROSS_AISLE,
) -> tuple[MetricResult, MetricResult]:
    """入库后验：同物料跨巷道 ≤ 5 且 同批跨巷道 ≤ 3（`15-00` §六，逐单口径）。

    两条指标都基于**入库后**的快照（`15-03` §8.1 时序说明）。达标 = 两口径都 PASS；
    任一口径超标即该口径 DEVIATION —— 两条各自判、各自落 `Verification`（`17` §4.4：
    入库有两条后验记录）。

    快照缺失抛 `BlockedMissingPrerequisite`（见模块 docstring）：缺快照是「没算」，不是「达标」。
    """
    if not snapshot.snapshot_present:
        raise BlockedMissingPrerequisite(
            "后验无库存快照（未导入或已过期）——入库跨巷道无从计算，不得当作达标",
            detail={"material_code": material_code, "batch_no": batch_no},
        )
    profile = snapshot.profile(material_code)
    material_count = profile.cross_aisle_count
    batch_count = _batch_aisle_count(snapshot, material_code, batch_no)
    return (
        MetricResult.judged(
            METRIC_MATERIAL_CROSS_AISLE,
            actual=material_count,
            threshold=material_threshold,
            passes=material_count <= material_threshold,
        ),
        MetricResult.judged(
            METRIC_BATCH_CROSS_AISLE,
            actual=batch_count,
            threshold=batch_threshold,
            passes=batch_count <= batch_threshold,
        ),
    )


def concentration_aisle_count(*, pick_qty_by_aisle: Mapping[str, int]) -> int:
    """拣货量加权集中度 = 按拣货量降序累加至 80% 所覆盖的巷道数（`15-00` §1.3）。

    巷道序无意义、只有「量」有意义：累加的是**降序后的拣货量**，故不管巷道叫什么，
    只看覆盖 80% 拣货量需要几条（`15-03` §8.1「对一张 DO 按拣货量降序累加至 80%」）。

    总拣货量为 0 时返回 0：一张没有拣货量的 DO 无从谈集中度，0 是「没覆盖任何巷道」，
    不是「1 条就够」—— 后者会让空单误判成完美集中。
    """
    total = sum(pick_qty_by_aisle.values())
    if total <= 0:
        return 0
    target = total * 0.8
    covered = 0
    for count, qty in enumerate(sorted(pick_qty_by_aisle.values(), reverse=True), start=1):
        covered += qty
        if covered >= target:
            return count
    # 浮点舍入兜底：若 80% 恰好等于 total，上面的循环必然在某一步达到；走到这里
    # 只能是 target 被 float 抬到比 total 略大 —— 覆盖全部巷道才算数。
    return len(pick_qty_by_aisle)


def verify_outbound(
    *, pick_qty_by_aisle: Mapping[str, int], n: int = DEFAULT_CONCENTRATION_N
) -> MetricResult:
    """出库后验：拣货量加权集中度 80% 落在 ≤ N 巷道（N=5，可配）。

    超标单**不阻断出库**（`15-03` §6.2 高亮放行）—— 判 `DEVIATION` 只作标记，
    改善靠入库收拢 + 移库补救（`15-03` §8.1）。单条指标，一行落 `Verification`。
    """
    actual = concentration_aisle_count(pick_qty_by_aisle=pick_qty_by_aisle)
    return MetricResult.judged(
        METRIC_CONCENTRATION,
        actual=actual,
        threshold=n,
        passes=actual <= n,
    )


def verify_relocate(
    *, cross_aisle_before: int, cross_aisle_after: int
) -> MetricResult:
    """移库后验：移库后同物料跨巷道数**低于**移库前（`15-04` §8.1，相对阈值）。

    下降即达标；持平或恶化（`后值 >= 前值`）即 DEVIATION。与入库 / 出库的绝对阈值
    （≤5 / ≤3 / ≤N）不同，移库比的是「比移库前更集中」（`15-04` §8.1 口径说明）。

    前后两个值由编排在台账写入前 / 后各自取好传入 —— 纯函数只做 `后 < 前` 这一个判断。
    """
    return MetricResult.judged(
        METRIC_RELOCATE_DROP,
        actual=cross_aisle_after,
        threshold=cross_aisle_before,
        passes=cross_aisle_after < cross_aisle_before,
    )


# ------------------------------------------------------------------ 编排层（会话 + 落库）

def _pick_qty_from_ledger(session: Session, job_order: JobOrder) -> dict[str, int]:
    """出库的拣货分布：优先从该单台账的 `pick_path_json`（`pick_sequence[]`）按 `aisle` 聚合
    `qty` → `{aisle: qty}`；`pick_path_json` 缺失/空回退 `source_location_code`（单巷，兼容
    无拣货路径的历史单源出库单，D5）。

    出库确认记录的拣货路径是**巷道粒度**（D7）：一份路径通常跨多条巷道，逐单的加权集中度
    因此真正按「80% 拣货量落到几条巷道」算（`verify_outbound`）；单源回退那条恒为 1（历史行）。
    台账既无拣货路径又无源库位（数据自相矛盾）时抛 `ValidationBlocked` → 编排迁
    `VERIFY_FAILED`，不静默 PASS。
    """
    ledger = session.scalars(
        sa.select(Ledger).where(
            Ledger.job_order_id == job_order.id, Ledger.is_reversal.is_(False)
        )
    ).first()
    if ledger is None:
        raise ValidationBlocked(
            "出库台账缺失——加权集中度无从计算，不得当作达标",
            detail={"job_order_id": job_order.id},
        )
    if ledger.pick_path_json:
        by_aisle: dict[str, int] = {}
        for entry in ledger.pick_path_json:
            by_aisle[entry["aisle"]] = by_aisle.get(entry["aisle"], 0) + entry["qty"]
        return by_aisle
    if ledger.source_location_code is not None:
        return {aisle_of(ledger.source_location_code): ledger.qty}
    raise ValidationBlocked(
        "出库台账既无拣货路径又无源库位——加权集中度无从计算，不得当作达标",
        detail={"job_order_id": job_order.id},
    )


def _compute_metrics(
    session: Session,
    job_order: JobOrder,
    snapshot: Snapshot | None,
    cross_aisle_before: int | None,
) -> list[MetricResult]:
    """按 `job_type` 分派到三口径纯函数，返回该单的全部 `MetricResult`。

    入库 / 移库都要「台账写入**后**」的库存分布，故这里从 `snapshot.id` **重新聚合**索引
    （`apply_increment` 已把增量 flush 进库存行，同事务内的 SELECT 看得到）—— 纯函数自己
    不读会话，取数这一下由编排补上。任一口径无法取数即抛 `DomainError` 子类：快照缺失抛
    `BlockedMissingPrerequisite`（阻断），缺移库前跨巷道抛 `ValidationBlocked`（迁
    `VERIFY_FAILED`）—— 由 `run_verification` 分派。
    """
    if job_order.job_type is JobType.INBOUND:
        index = load_snapshot_index(
            session, snapshot_id=snapshot.id if snapshot is not None else None
        )
        return list(
            verify_inbound(
                material_code=job_order.material_code,
                batch_no=job_order.batch_no or "",
                snapshot=index,
            )
        )
    if job_order.job_type is JobType.OUTBOUND:
        return [verify_outbound(pick_qty_by_aisle=_pick_qty_from_ledger(session, job_order))]
    # RELOCATE
    if cross_aisle_before is None:
        raise ValidationBlocked(
            "移库后验缺少移库前跨巷道数——无从比较，不得当作达标",
            detail={"job_order_id": job_order.id},
        )
    index = load_snapshot_index(
        session, snapshot_id=snapshot.id if snapshot is not None else None
    )
    if not index.snapshot_present:
        raise BlockedMissingPrerequisite(
            "移库后验无库存快照——跨巷道无从计算，不得当作达标",
            detail={"job_order_id": job_order.id},
        )
    after = index.profile(job_order.material_code).cross_aisle_count
    return [verify_relocate(cross_aisle_before=cross_aisle_before, cross_aisle_after=after)]


def _write_verifications(
    session: Session, job_order: JobOrder, metrics: list[MetricResult]
) -> None:
    """把后验结果落成 `Verification` 行 —— 一行一条指标（`17` §4.4，`(job_order, metric_kind)`
    唯一）。达标 / 偏离都落：`verify_result` 已把结论带在每条 `MetricResult` 上。"""
    for metric in metrics:
        session.add(
            Verification(
                warehouse_id=job_order.warehouse_id,
                job_order_id=job_order.id,
                metric_kind=metric.metric_kind,
                actual_value=metric.actual_value,
                threshold_value=metric.threshold_value,
                verify_result=metric.verify_result,
            )
        )
    session.flush()


def _deviation_cause(job_type: JobType) -> DeviationCauseKind:
    """偏离成因的 v1 默认分派。

    成因区分「新入库收拢不达标 vs 历史库存拖累」首期靠操作员人工判断（`15-04` §4.1），
    后验自动打标时只能按作业类型给一个**可被后续人工修订的默认值**：
    入库偏离 → 新入库收拢不达标（本次落位没把料收进既有巷道，超标是这次动作造成的）；
    出库 / 移库偏离 → 历史库存拖累（散射是存量分布，不是本次出库 / 移库造成的）。
    """
    if job_type is JobType.INBOUND:
        return DeviationCauseKind.NEW_INBOUND_SHORTFALL
    return DeviationCauseKind.LEGACY_INVENTORY_DRAG


def _write_deviations(
    session: Session, job_order: JobOrder, metrics: list[MetricResult]
) -> None:
    """偏离标记：`verify_result = DEVIATION` 的指标各落一条 `Deviation`，`PASS` 不落（17 §4.4）。

    一行一条偏离，标识 = 物料 + 批号（`material_code` 恒在，`identifier_required` 由它满足；
    批号在台账写入后也必在）。`actual/threshold_cross_aisle` 取该指标自身的实测 / 阈值，
    **整数化** —— 三口径的实测值都是整数巷道数，`Verification` 那两列按 Float 存只为容纳
    `4.8` 这类小数。入库同物料与同批两条都偏离时各落一条（阈值 5 vs 3 可区分两行）——
    不合并成一条，否则会丢掉「哪一口径超标」这个事实。成因按作业类型给 v1 默认值，
    处置路径由操作员后续修订（`15-04` §4.1）。
    """
    for metric in metrics:
        if not metric.is_deviation:
            continue
        session.add(
            Deviation(
                warehouse_id=job_order.warehouse_id,
                batch_no=job_order.batch_no,
                material_code=job_order.material_code,
                actual_cross_aisle=int(metric.actual_value),
                threshold_cross_aisle=int(metric.threshold_value),
                cause_kind=_deviation_cause(job_order.job_type),
            )
        )
    session.flush()


def run_verification(
    session: Session,
    *,
    job_order: JobOrder,
    snapshot: Snapshot | None = None,
    cross_aisle_before: int | None = None,
) -> JobOrder:
    """后验编排：`EXECUTED → VERIFYING → VERIFIED / VERIFY_FAILED`，同请求同步完成。

    也**就是**重试编排：`assert_transition(当前, VERIFYING)` 同时收 `EXECUTED → VERIFYING`
    与 `VERIFY_FAILED → VERIFYING` 两条边（`15-01` §3.1），故确认链与 `/retry` 端点调用的是
    同一个函数 —— 「无放弃后验终态」由「不存在别的后验入口」兑现。

    事务结构镜像 `confirm._confirm_and_execute`：`VERIFYING` 在 try **之外** flush（这样
    savepoint 回滚后 DB 上仍是 `VERIFYING`，`VERIFY_FAILED` 的回边才有合法起点）；计算 +
    落 `Verification` + `VERIFIED` 包在 `begin_nested()` 里，任一步失败只滚这一段，
    台账 / cap 增量留在外层事务 —— 后验失败不推翻已执行的事实（`15-01` §3.3.2「作业单已
    EXECUTED、台账已正确，后验仅作度量」）。失败分两种：快照缺失**阻断**上抛（409），
    数据自相矛盾才落 `VERIFY_FAILED` 待重试。

    返回**同一个** `job_order`（`VERIFIED` 或 `VERIFY_FAILED`）。不 `commit`：事务边界属于
    端点 / 确认链（与 `_confirm_and_execute` 同一口径）。
    """
    job_order.status = assert_transition(job_order.status, JobStatus.VERIFYING)
    session.flush()
    try:
        with session.begin_nested():
            metrics = _compute_metrics(session, job_order, snapshot, cross_aisle_before)
            _write_verifications(session, job_order, metrics)
            _write_deviations(session, job_order, metrics)
            job_order.status = assert_transition(job_order.status, JobStatus.VERIFIED)
            session.flush()
    except BlockedMissingPrerequisite:
        # 快照缺失 → 阻断（409），不迁 VERIFY_FAILED（CLAUDE.md §四「阻断并提示重新导入」）。
        # 上抛让调用方（重试端点）据此拒绝；`VERIFYING` 已在 savepoint 外 flush，外层事务
        # 回滚后单子回到原状态（EXECUTED / VERIFY_FAILED）。
        raise
    except DomainError:
        # 数据自相矛盾等「算不出来」→ VERIFY_FAILED，可重试；savepoint 已回滚，DB 上
        # 回到 VERIFYING；内存态可能残留 VERIFIED，先 expire 回库。
        session.expire(job_order)
        job_order.status = assert_transition(job_order.status, JobStatus.VERIFY_FAILED)
        session.flush()
    except Exception:
        # 非预期（DB 错误 / bug）不静默吞 —— 上抛，让事务边界按失败处理（S7）。
        raise
    return job_order
