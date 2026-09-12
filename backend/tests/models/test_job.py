"""作业链 5 实体的契约测试（tasks.md §4 的验证）。

事实来源：17-数据模型设计 §四（作业与交易实体）、§10.1~10.3（三类方案 JSON）、§10.6（KPI 卡片）
          15-入库出库移库与后验流程设计 §6.2/§6.3（批量处置与写台账）、§7.1（后验口径）、
            附录A（三类台账字段矩阵）
          openspec/changes/data-model-permission/design.md D1、D2、D3、D4、D8、D12
          spec `data-model`「JobOrder 状态机」「枚举登记范围与取值」「乐观锁并发守卫」

**CHECK 类用例分两种写法，理由不同**：

  - **取值类**（枚举列的 CHECK）用原生 `text()` 发 SQL。D3 的两层约束里，Python 那层
    （`validate_strings`）在绑定参数时就抛了，ORM 路径**从没碰过** DB 的 CHECK ——
    而绕过 ORM 的写入（迁移脚本、手工 SQL、将来的批量导入）正是 CHECK 存在的理由。
  - **结构类**（长度 / 条件必填 / 按类型互斥）用 ORM 写。这类约束 Python 层根本不拦
    （SQLite 忽略 `VARCHAR(n)` 的长度），ORM 路径同样会走到 DB，所以用例可以直接写
    「正常人会怎么写」，可读性更好。

**本组用例里的取值都来自文档**：PO 号 `3573743144K55G` 与行号 `10` 取自 16 附录A 的字段
映射示例，DO 号 `DO-88` 取自 17 §10.2，料号 / 品名 / 批号取自 PRD 8.3.1 的真实样本，
6 因子权重与分值取自 17 §10.1，跨巷道阈值 5 / 3 取自 15 §7.1。只有 `qty=40` 与
`bulk_batch_no` 是合成值 —— 文档没有给 PO 行的数量样本与批量批次号格式，注释里已注明。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.concurrency import assert_lock_version, bump_lock_version
from app.core.enums import (
    AbcClass,
    AccountStatus,
    Disposition,
    JobStatus,
    JobType,
    LedgerType,
    Role,
    VerifyResult,
)
from app.core.errors import StateConflict
from app.models.base import Base
from app.models.identity import Account
from app.models.job import (
    Deviation,
    DeviationCauseKind,
    DeviationStatus,
    JobOrder,
    Ledger,
    PlanKind,
    RecommendationPlan,
    Verification,
)

pytestmark = pytest.mark.model

#: 首期单厂（app/core/config.py 的 warehouse_code）。
WAREHOUSE = "GTJ10036"

#: 原生 INSERT 用：created_at / updated_at 无 server_default，必须显式给。
NOW = "2026-09-11 08:00:00"

#: 单据与行：16 附录A 字段映射表的 `order_no` / `line_no` 示例值。
ORDER_NO = "3573743144K55G"
LINE_NO = "10"
DO_NO = "DO-88"

#: 物料 / 批号：PRD 8.3.1 的真实样本。
MATERIAL_CODE = "3001234"
MATERIAL_NAME = "PET500 茉莉柚茶"
BATCH_NO = "GJP2571221"

#: 批量批次号 —— **合成值**：15 §3.3 只说「一次批量生成一个批次」，未给格式。
BULK_BATCH_NO = "BULK-20260908-01"

#: 账号用户名 —— **合成值**：文档只给字段不给样本（13 §5.2）。
ACCOUNT_USERNAME = "gtj_keeper"

#: 17 §10.1 的入库推荐理由（6 因子权重的取值原样照抄）。
REASONS_JSON = {
    "job_id": "JOB-20260908-001",
    "aisles": ["01", "02"],
    "factors": {
        "abc": 0.25, "cap": 0.20, "existing": 0.15,
        "station": 0.20, "batch": 0.10, "continuity": 0.10,
    },
    "scores": {"01": 0.86, "02": 0.81, "21": 0.42},
    "degraded": False,
    "degrade_reason": None,
}


# ------------------------------------------------------------------ 夹具工厂
# 按依赖链顺序建父表记录（D12）：JobOrder → {Plan / Ledger / Verification}，Deviation 另挂 JobOrder。

def _job_order(session: Session, **overrides) -> JobOrder:
    """作业单。默认值 = 一张入库 PO 行（16 附录A 的示例单据）。"""
    fields = {
        "order_no": ORDER_NO,
        "line_no": LINE_NO,
        "job_type": JobType.INBOUND,
        "material_code": MATERIAL_CODE,
        "material_name": MATERIAL_NAME,
        "qty": 40,  # 合成值：文档未给 PO 行的数量样本
        "abc_class": AbcClass.A,
        "batch_no": BATCH_NO,
        "status": JobStatus.PENDING,
    }
    fields.update(overrides)
    row = JobOrder(warehouse_id=WAREHOUSE, **fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _plan(session: Session, job_order: JobOrder, **overrides) -> RecommendationPlan:
    """推荐方案。默认值 = 入库的「分配」方案 + 17 §10.1 的推荐理由。"""
    fields = {
        "plan_kind": PlanKind.ASSIGN,
        "payload_json": dict(REASONS_JSON),
        "degraded": False,
        "degrade_reason": None,
    }
    fields.update(overrides)
    row = RecommendationPlan(
        warehouse_id=WAREHOUSE, job_order_id=job_order.id, **fields  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


def _account(session: Session) -> Account:
    """账号。台账的操作人与作业单的确认人共用它（§5 起这两列是**真外键**）。

    先查后建：一个用例里可能造多张台账，各建一个账号会撞 `username` 的全局唯一约束。
    """
    existing = session.execute(
        select(Account).where(Account.username == ACCOUNT_USERNAME)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = Account(
        warehouse_id=WAREHOUSE,
        username=ACCOUNT_USERNAME,
        # bcrypt 哈希的真实形态（60 字符）；这里不求可验证，只要形态对。
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(row)
    session.flush()
    return row


def _ledger(session: Session, job_order: JobOrder, **overrides) -> Ledger:
    """台账。默认值 = 一张入库台账：**只有目标库位，没有源库位**（15 附录A 的字段矩阵）。"""
    fields = {
        "ledger_type": LedgerType.INBOUND,
        "order_no": ORDER_NO,
        "material_code": MATERIAL_CODE,
        "material_name": MATERIAL_NAME,
        "batch_no": BATCH_NO,
        "qty": 40,
        # 操作人是必填（15 附录A 三类台账都 ✅）。§4 时这里填的是占位整数 1
        # （账号表尚未落地）；§5 补上外键后必须指向**真实账号**。
        "operator_id": _account(session).id,
        "source_location_code": None,
        "target_location_code": "010104",
        "executed_at": datetime(2026, 9, 8, 8, 0),
        "degraded": False,
    }
    fields.update(overrides)
    row = Ledger(
        warehouse_id=WAREHOUSE, job_order_id=job_order.id, **fields  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


def _verification(session: Session, job_order: JobOrder, **overrides) -> Verification:
    """后验记录。默认值 = 15 §7.1 的入库主口径「同物料跨巷道数 ≤ 5」，实测 4 达标。"""
    fields = {
        "metric_kind": "同物料跨巷道",
        "actual_value": 4.0,
        "threshold_value": 5.0,
        "verify_result": VerifyResult.PASS,
    }
    fields.update(overrides)
    row = Verification(
        warehouse_id=WAREHOUSE, job_order_id=job_order.id, **fields  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


def _deviation(session: Session, **overrides) -> Deviation:
    """偏离批次。默认值 = 17 §4.4 的字段清单 + 15 §7.2 的成因分类。"""
    fields = {
        "batch_no": BATCH_NO,
        "material_code": None,
        "actual_cross_aisle": 6,
        "threshold_cross_aisle": 3,
        "cause_kind": DeviationCauseKind.NEW_INBOUND_SHORTFALL,
        "status": DeviationStatus.OPEN,
    }
    fields.update(overrides)
    row = Deviation(warehouse_id=WAREHOUSE, **fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------ 注册与建表

def test_job_tables_are_registered() -> None:
    """5 张表必须都进 `Base.metadata`（漏登记的表在真实库里根本不会被建）。"""
    assert {
        "job_orders",
        "recommendation_plans",
        "ledgers",
        "verifications",
        "deviations",
    } <= set(Base.metadata.tables)


def test_no_soft_delete_columns() -> None:
    """spec `data-model`：不得引入软删除列（17 §十二用「归档不删除 + 版本化」）。"""
    for name in (
        "job_orders",
        "recommendation_plans",
        "ledgers",
        "verifications",
        "deviations",
    ):
        cols = {c.name for c in Base.metadata.tables[name].columns}
        assert "is_deleted" not in cols
        assert "deleted_at" not in cols


def test_foreign_keys_are_enforced(foreign_keys_on: bool) -> None:
    """前置断言：本组全部外键用例的前提是 PRAGMA 真的开了（D12）。"""
    assert foreign_keys_on is True


# ------------------------------------------------------------------ 4.1 JobOrder

def test_job_order_defaults(session: Session) -> None:
    """新建单子 = 待处理：还没方案、没确认、没落位（15 §3.2 的 PENDING 行）。"""
    row = _job_order(session)

    assert row.status is JobStatus.PENDING
    assert row.lock_version == 0
    assert row.disposition is None
    assert row.confirmed_at is None
    assert row.actual_location_code is None
    assert row.actual_qty is None
    assert row.executed_at is None
    assert row.created_at is not None
    assert row.updated_at is not None


def test_job_status_accepts_all_seven_values(session: Session) -> None:
    """7 个状态全部可写，且**存的是取值**（15 §3.1 / 17 §九②）。"""
    for index, status in enumerate(JobStatus):
        _job_order(session, order_no=f"PO-{index}", line_no="1", status=status)
    session.flush()

    stored = set(session.execute(text("SELECT status FROM job_orders")).scalars().all())
    assert stored == {s.value for s in JobStatus}


def test_job_status_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层：`Enum(validate_strings=True)` 在绑定参数时即拒（抛 `StatementError`）。

    tasks 4.1 的验证动作「断言非法 `job_status` 被拒」的 Python 侧。
    """
    with pytest.raises(StatementError):
        _job_order(session, status="NOT_A_STATUS")  # type: ignore[arg-type]


def test_job_status_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：绕过 ORM 的原生写入由 DB 的 CHECK 拒绝。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO job_orders "
                "(warehouse_id, order_no, line_no, job_type, material_code, qty, "
                " status, lock_version, created_at, updated_at) "
                "VALUES (:w, 'PO-RAW', '1', 'INBOUND', :m, 1, 'NOT_A_STATUS', 0, :now, :now)"
            ),
            {"w": WAREHOUSE, "m": MATERIAL_CODE, "now": NOW},
        )
    session.rollback()


def test_job_type_rejected_by_db_check(session: Session) -> None:
    """`job_type` 限三类（17 §九①）—— 三类作业共用一张表，取值错了就分错了流。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO job_orders "
                "(warehouse_id, order_no, line_no, job_type, material_code, qty, "
                " status, lock_version, created_at, updated_at) "
                "VALUES (:w, 'PO-RAW', '1', 'TRANSFER', :m, 1, 'PENDING', 0, :now, :now)"
            ),
            {"w": WAREHOUSE, "m": MATERIAL_CODE, "now": NOW},
        )
    session.rollback()


def test_abc_class_rejected_by_db_check(session: Session) -> None:
    """`abc_class` 限 A/B/C（17 §九⑦）—— 它决定预留池使用权，取值域不能开口。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO job_orders "
                "(warehouse_id, order_no, line_no, job_type, material_code, qty, abc_class, "
                " status, lock_version, created_at, updated_at) "
                "VALUES (:w, 'PO-RAW', '1', 'INBOUND', :m, 1, 'D', 'PENDING', 0, :now, :now)"
            ),
            {"w": WAREHOUSE, "m": MATERIAL_CODE, "now": NOW},
        )
    session.rollback()


def test_disposition_rejected_by_db_check(session: Session) -> None:
    """`disposition` 限 ACCEPT / TUNE / REJECT（17 §九⑥，逐单处置）。

    17 §4.5 的「操作与确认记录」不是 23 实体之一，处置结果就落在这张表上 ——
    取值放开口，等于把「操作员到底怎么处置的」变成自由文本。
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO job_orders "
                "(warehouse_id, order_no, line_no, job_type, material_code, qty, disposition, "
                " status, lock_version, created_at, updated_at) "
                "VALUES (:w, 'PO-RAW', '1', 'INBOUND', :m, 1, 'IGNORE', 'PENDING', 0, :now, :now)"
            ),
            {"w": WAREHOUSE, "m": MATERIAL_CODE, "now": NOW},
        )
    session.rollback()


def test_order_line_is_unique_within_a_job_type(session: Session) -> None:
    """16 附录A：「行唯一键（PO / DO）= 单据号码 + 行号 联合唯一」。

    一张 PO 有多行，每行一个物料 —— 重复导入同一行必须被拒，否则入库队列里会出现
    两条一模一样的待推荐单，批量分配会把同一批货分配两次。
    """
    _job_order(session)

    with pytest.raises(IntegrityError):
        _job_order(session)
    session.rollback()


def test_same_order_line_allowed_across_job_types(session: Session) -> None:
    """唯一键带上 `job_type`：三类单据的号码来自三个编制方（生产 / 发货 / 本系统），
    跨类撞号是误判而非真重复（16 附录A 的联合唯一是**同一文件内**的判重口径）。"""
    _job_order(session)
    _job_order(session, job_type=JobType.RELOCATE, status=JobStatus.PENDING)
    session.flush()

    assert session.execute(text("SELECT count(*) FROM job_orders")).scalar_one() == 2


def test_lock_version_guard_works_on_job_order(session: Session) -> None:
    """`JobOrder` 必须能直接进 §1 的乐观锁守卫（D1：乐观锁名单 = JobOrder / ImportSession）。

    15 §10.6 把「防止多端同时确认同一作业单」列为工程参数；红线「`EXECUTED` 后不重复
    写台账」是状态机守卫 + 乐观锁的双保险。
    """
    row = _job_order(session)

    assert bump_lock_version(row, 0) == 1
    with pytest.raises(StateConflict):
        assert_lock_version(row, 99)


def test_job_order_has_no_business_version_column() -> None:
    """D1 的版本语义三分：作业单只有**乐观锁**，没有配置型业务版本。

    `version_no` 属 `WeightConfig` / `CapacityConfig`（配置型业务版本，对用户可见）。
    作业单一旦多出一个 `version_no`，最典型的故障是把乐观锁当业务版本读。
    """
    cols = set(Base.metadata.tables["job_orders"].c.keys())
    assert "lock_version" in cols
    assert "version_no" not in cols
    assert "version" not in cols


def test_actual_location_code_must_be_six_chars(session: Session) -> None:
    """库位号按 6 位文本处理（spec `data-model`「库位号按 6 位文本处理」）。

    落位写进作业单的实际库位同样受这一条管：`010104` 是 6 位，`10104` 是把前导 0
    弄丢以后的形状 —— 那种值一旦落库，巷道切片 `[:2]` 会算出 `10` 而不是 `01`。
    """
    row = _job_order(session)

    row.actual_location_code = "10104"  # 5 位：前导 0 丢了
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_actual_location_code_is_nullable_before_execution(session: Session) -> None:
    """`PENDING` 的单子还没有实际库位 —— 列必须可空，否则队列根本建不起来。"""
    row = _job_order(session)
    assert row.actual_location_code is None

    row.actual_location_code = "010104"
    session.flush()
    assert row.actual_location_code == "010104"


def test_batch_no_is_nullable(session: Session) -> None:
    """批号可空：16 附录A 的 PO / DO 模版**都没有批号列**（PO 只有生产日期）。

    入库单的批号由**系统在入库单建立时按生产批规则生成**（同一生产批共用同一批号，
    `17` §2.4 —— 生成规则本身属阶段四，故本阶段只保证列可空）；出库单的批号由顺路取
    从库存明细里选出（17 §10.2 的 `pick_sequence[].batches`）。17 §4.1 把它列在
    「物料信息」里，故列存在、但此刻可空。
    """
    row = _job_order(session, batch_no=None)
    assert row.batch_no is None


def test_bulk_batch_no_is_nullable_until_a_batch_runs(session: Session) -> None:
    """批量批次号可空：单子由**导入**入队时还没有批量（17 §4.1「所属批量批次 ID」）。"""
    row = _job_order(session)
    assert row.bulk_batch_no is None

    row.bulk_batch_no = BULK_BATCH_NO
    session.flush()
    assert row.bulk_batch_no == BULK_BATCH_NO


def test_recommendation_and_verification_columns_live_on_their_own_tables() -> None:
    """17 §4.1 的「推荐侧 / 后验侧」**不是**作业单上的列。

    §4.1 是按聚合根罗列的属性分组，§4.2 / §4.4 才把它们各自定义成实体。同一事实在
    两处各存一份，就等于有了两个可写的副本 —— 而「推荐理由 → 确认记录 → 实际落位 →
    后验结果」这条追溯链（15 §7.3）指向的是哪一份，将没有答案。
    """
    cols = set(Base.metadata.tables["job_orders"].c.keys())
    for leaked in (
        "recommended_aisles",
        "payload_json",
        "degrade_reason",
        "degraded",
        "cross_aisle",
        "metric_kind",
        "verify_result",
    ):
        assert leaked not in cols, f"{leaked} 应落在 RecommendationPlan / Verification 上"


def test_updated_at_advances_on_change(session: Session) -> None:
    """作业单会被反复改状态（15 §3.2 的 7 态流转）—— 没有 `updated_at` 就查不出
    「这张单最后被动过是什么时候」，而排查并发问题时那是第一个要看的值。"""
    row = _job_order(session)
    first = row.updated_at

    row.status = JobStatus.PLANNED
    session.flush()
    session.expire_all()
    assert row.updated_at >= first


# ------------------------------------------------------------------ 4.3 RecommendationPlan

def test_plan_kind_values_match_the_doc() -> None:
    """方案类型是**实体局部值域**（D2），取值照 17 §4.2 原文：分配 / 顺路取 / 收拢。"""
    assert {k.value for k in PlanKind} == {"分配", "顺路取", "收拢"}
    assert PlanKind.__module__ == "app.models.job"


def test_plan_kind_rejected_by_db_check(session: Session) -> None:
    """局部值域同样受 D3 的两层约束 —— 不建枚举就等于取值只活在散文里。"""
    job = _job_order(session)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO recommendation_plans "
                "(warehouse_id, job_order_id, plan_kind, payload_json, degraded, created_at) "
                "VALUES (:w, :j, 'NOT_A_KIND', '{}', 0, :now)"
            ),
            {"w": WAREHOUSE, "j": job.id, "now": NOW},
        )
    session.rollback()


def test_plan_payload_round_trips_as_the_documented_shape(session: Session) -> None:
    """17 §10.1 的推荐理由结构原样可存可读（D4：JSON 列 + 引擎级序列化器）。"""
    plan = _plan(session, _job_order(session))
    session.expire_all()

    payload = plan.payload_json
    assert payload["factors"]["abc"] == 0.25
    assert payload["scores"] == {"01": 0.86, "02": 0.81, "21": 0.42}
    assert payload["degraded"] is False


def test_three_plan_kinds_share_one_payload_column(session: Session) -> None:
    """D4：三类作业方案共用 `payload_json`，不拆三列。

    17 §10.2 的顺路取顺序与 §10.3 的收拢方案都落在这里 —— 依据是 26 Step 4 的
    「`JobOrder` 三类共用 `job_type` 区分」口径。
    """
    outbound = _job_order(session, job_type=JobType.OUTBOUND, order_no=DO_NO, line_no="1")
    relocate = _job_order(session, job_type=JobType.RELOCATE, order_no="MV-88", line_no="1")

    pick_sequence = {"do_no": DO_NO, "pick_sequence": [{"aisle": "01", "qty": 3}], "exceeded": False}
    consolidation = {"batch_no": BATCH_NO, "target_aisle": "01", "plates": 40, "batch_unchanged": True}

    _plan(session, outbound, plan_kind=PlanKind.PICK, payload_json=pick_sequence)
    _plan(session, relocate, plan_kind=PlanKind.CONSOLIDATE, payload_json=consolidation)
    session.flush()
    session.expire_all()

    stored = {
        row.plan_kind: row.payload_json
        for row in session.execute(select(RecommendationPlan)).scalars()
    }
    assert stored[PlanKind.PICK]["pick_sequence"][0]["aisle"] == "01"
    assert stored[PlanKind.CONSOLIDATE]["batch_unchanged"] is True


def test_degraded_requires_a_reason(session: Session) -> None:
    """tasks 4.3 的验证动作：**降级不静默**（17 §10.1「降级时 `degraded=true` 且
    `degrade_reason` 必填」）。

    CHECK 只钉一个方向：降级 ⇒ 必有原因。反方向（记了原因却没标降级）不拦 ——
    14 §4.4 的降级链按因子逐条触发，「部分因子降级、整体未降级」是可能出现的取值，
    DB 拦死会需要一次迁移才能放开，而它并不是红线要防的东西。
    """
    plan = _plan(session, _job_order(session))

    plan.degraded = True
    plan.degrade_reason = None
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_degraded_plan_accepts_a_reason(session: Session) -> None:
    """降级 + 写了原因 → 放行（降级不静默的**可表达**形式）。"""
    plan = _plan(session, _job_order(session))

    plan.degraded = True
    plan.degrade_reason = "站台距离权重缺失，站台因子降级为默认值"
    session.flush()
    session.expire_all()
    assert plan.degrade_reason is not None


def test_not_degraded_may_have_a_null_reason(session: Session) -> None:
    """17 §10.1 的样本形状（`false` / `null`）必须存得进去 —— 正常路径不该被约束挡住。"""
    plan = _plan(session, _job_order(session))
    assert plan.degraded is False
    assert plan.degrade_reason is None


def test_plan_kind_pairs_with_job_type(session: Session) -> None:
    """方案类型与作业类型是**一一对应**的：入库→分配、出库→顺路取、移库→收拢。

    这一条把 D2 的决定变成可执行的断言：既然两者可互推，局部值域存在的理由就不是
    「表达新信息」，而是「让错配可被拒绝」—— 入单单子上挂一个「顺路取」方案，
    在批量方案表里会显示成另一类作业的方案。
    """
    pairing = {
        JobType.INBOUND: PlanKind.ASSIGN,
        JobType.OUTBOUND: PlanKind.PICK,
        JobType.RELOCATE: PlanKind.CONSOLIDATE,
    }
    assert len(set(pairing.values())) == len(pairing)  # 一一对应，无重复

    for index, (job_type, kind) in enumerate(pairing.items()):
        job = _job_order(session, job_type=job_type, order_no=f"DOC-{index}", line_no="1")
        plan = _plan(session, job, plan_kind=kind)
        assert plan.plan_kind is kind


def test_a_job_order_may_be_replanned(session: Session) -> None:
    """`job_order_id` **不唯一**：15 §3.1 允许 `PENDING → PENDING`（分配失败重试）与
    `REJECTED → PENDING`（驳回后重新入队）—— 同一张单会有多份方案，历史保留可追溯。

    「当前方案」不加指针列（D1 对配置版本用同一手法）：取该单 `id` 最大的一行。
    """
    job = _job_order(session)
    first = _plan(session, job)
    second = _plan(session, job, degraded=True, degrade_reason="站台主数据缺失")

    current = session.execute(
        select(RecommendationPlan)
        .where(RecommendationPlan.job_order_id == job.id)
        .order_by(RecommendationPlan.id.desc())
    ).scalars().first()

    assert current is not None and current.id == second.id
    assert first.id != second.id  # 旧方案没被覆盖，仍在表里


def test_plan_has_no_business_version_column() -> None:
    """与 `JobOrder` 同一条 D1 口径：方案是**追加**的，不靠 `version_no` 排队。"""
    cols = set(Base.metadata.tables["recommendation_plans"].c.keys())
    assert "generated_at" not in cols  # 生成时间 = created_at（17 §4.2）
    assert "version_no" not in cols
    assert "lock_version" not in cols  # 乐观锁名单只有 JobOrder / ImportSession


# ------------------------------------------------------------------ 4.4 Ledger

def test_ledger_type_reuses_the_job_type_value_domain() -> None:
    """17 §九⑤ 明写 `ledger_type` **复用** `job_type` 取值域 —— 两处取值必须始终相等。

    台账与作业单的 `*_TYPE` 一旦漂移，按类型聚合的 KPI（18 号）会把同一类作业算成两拨。
    """
    assert {t.value for t in LedgerType} == {t.value for t in JobType}
    assert LedgerType.__module__ == "app.core.enums"  # 共享枚举，不在实体模块里


def test_second_ledger_for_the_same_job_order_is_rejected(session: Session) -> None:
    """tasks 4.4 的验证动作：**同一作业单的第二条台账写入被拒**。

    红线有二：「台账只有一套」、「`EXECUTED` 后不允许重复写台账」（15 §11.7）。
    状态机守卫挡的是「正常路径上再走一次迁移」；这里是**结构层**的兜底 ——
    网络重试、并发确认、绕过状态机的手工写入，撞到的都是这个唯一约束。

    唯一键落在 `job_order_id` 而不是（`ledger_type`, `order_no`）：一张 PO 有多行
    （16 附录A 的行唯一键 = 单据号码 + 行号），而台账按 15 附录A **不记行号** ——
    用（类型, 单号）做键会让多行单据的第二行写不进去，把「防重复」变成「防多行」。
    """
    job = _job_order(session)
    _ledger(session, job)

    with pytest.raises(IntegrityError):
        _ledger(session, job)
    session.rollback()


def test_ledger_type_rejected_by_db_check(session: Session) -> None:
    """台账类型同样受 D3 两层约束（17 §九⑤）。

    **这条用例刻意不点名是哪条 CHECK**：第四个类型必然同时违反两条 —— 库位矩阵那条
    （`ck_ledgers_location_columns_by_type` 的三个析取项全按三个合法类型写死）与类型
    那条。命中顺序由 DDL 里约束的先后决定，钉死它等于把约束的书写顺序变成契约。
    要点是「绕过 ORM 的写入被库层拒绝」，故断言约束**出自 ledgers**。

    `operator_id` 指向真账号（§5 起有外键）—— 否则这一行会先撞外键，
    用例就变成了在测外键而不是在测取值约束。
    """
    job = _job_order(session)
    account = _account(session)

    with pytest.raises(IntegrityError) as excinfo:
        session.execute(
            text(
                "INSERT INTO ledgers "
                "(warehouse_id, job_order_id, ledger_type, order_no, material_code, batch_no, "
                " qty, operator_id, executed_at, degraded, created_at) "
                "VALUES (:w, :j, 'TRANSFER', 'PO-RAW', :m, :b, 1, :op, :now, 0, :now)"
            ),
            {
                "w": WAREHOUSE,
                "j": job.id,
                "m": MATERIAL_CODE,
                "b": BATCH_NO,
                "op": account.id,
                "now": NOW,
            },
        )

    assert "ck_ledgers_" in str(excinfo.value)
    session.rollback()


def test_ledger_location_codes_must_be_six_chars(session: Session) -> None:
    """源 / 目标库位同样是 6 位文本（spec「库位号按 6 位文本处理」）。"""
    job = _job_order(session)
    row = _ledger(session, job)

    row.target_location_code = "10104"
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_ledger_source_and_target_follow_the_type_matrix(session: Session) -> None:
    """15 附录A 的三类台账字段矩阵：入库无源库位、出库无目标库位、移库两者都有。

    把矩阵写成 CHECK 而不是留给服务层自觉，是因为「入库台账里冒出一个源库位」
    在按巷道汇总 cap 增量（16 §6.3）时会被当成一次出库，静默地多减一格。
    """
    # 入库：只有目标库位（默认工厂即此形状）
    inbound = _job_order(session)
    _ledger(session, inbound)

    # 出库：有源库位、无目标库位
    outbound = _job_order(session, job_type=JobType.OUTBOUND, order_no=DO_NO, line_no="1")
    _ledger(
        session,
        outbound,
        ledger_type=LedgerType.OUTBOUND,
        order_no=DO_NO,
        source_location_code="010104",
        target_location_code=None,
    )

    # 移库：两者都有
    relocate = _job_order(session, job_type=JobType.RELOCATE, order_no="MV-88", line_no="1")
    _ledger(
        session,
        relocate,
        ledger_type=LedgerType.RELOCATE,
        order_no="MV-88",
        source_location_code="211202",
        target_location_code="010104",
    )
    session.flush()

    # 反例一：入库台账带了源库位
    bad_inbound = _job_order(session, order_no="PO-BAD", line_no="1")
    with pytest.raises(IntegrityError):
        _ledger(session, bad_inbound, source_location_code="010104")
    session.rollback()

    # 反例二：移库台账少了目标库位（只调整所在巷道，但源与目标都必须记）
    bad_relocate = _job_order(session, job_type=JobType.RELOCATE, order_no="MV-BAD", line_no="1")
    with pytest.raises(IntegrityError):
        _ledger(
            session,
            bad_relocate,
            ledger_type=LedgerType.RELOCATE,
            order_no="MV-BAD",
            source_location_code="211202",
            target_location_code=None,
        )
    session.rollback()


def test_ledger_index_covers_the_ledger_query() -> None:
    """design.md 性能目标表：落位写入（SLA ≤200ms）走 `(warehouse_id, order_no)` 索引。

    这里把索引的列序钉住 —— 调序后索引还在，只是不再被查询命中，属于「改了没报错、
    只是慢了」的那类退化（`GET /api/ledger?type=...`，15 附录B）。
    """
    (index,) = list(Base.metadata.tables["ledgers"].indexes)
    assert [col.name for col in index.columns] == ["warehouse_id", "order_no"]


def test_pick_path_json_round_trips(session: Session) -> None:
    """15 附录A：拣货路径（巷道序）只属出库台账，是**数组**结构 → JSON 列。

    D4 的落列表只映射 17 §10 的 6 类结构，未列台账的拣货路径；这里与
    `ImportSession.files_json` 同一处理：文档要求这组数据在库里，就不是「多加了列」。
    """
    job = _job_order(session, job_type=JobType.OUTBOUND, order_no=DO_NO, line_no="1")
    row = _ledger(
        session,
        job,
        ledger_type=LedgerType.OUTBOUND,
        order_no=DO_NO,
        source_location_code="010104",
        target_location_code=None,
        pick_path_json=["01", "05", "21"],
    )
    session.expire_all()
    assert row.pick_path_json == ["01", "05", "21"]


def test_ledger_batch_no_is_always_recorded(session: Session) -> None:
    """15 附录A：批号三类台账都 ✅ —— **移库不改批号**，只记录。

    「只调整所在巷道」（17 §4.3 表注）在表形状上的痕迹就是：台账永远写着批号，
    而模型里没有任何一列可以改写它。
    """
    cols = Base.metadata.tables["ledgers"].c
    assert cols.batch_no.nullable is False

    job = _job_order(session, job_type=JobType.RELOCATE, order_no="MV-88", line_no="1")
    row = _ledger(
        session,
        job,
        ledger_type=LedgerType.RELOCATE,
        order_no="MV-88",
        source_location_code="211202",
        target_location_code="010104",
    )
    assert row.batch_no == BATCH_NO


def test_ledger_has_no_verification_columns() -> None:
    """15 附录A 给台账列了「后验结果」，但**17 §4.3 的台账字段矩阵没有这一行** ——
    17 是实体字段的单一事实来源（附录A 自己也这么写），故后验结论落在 `verifications`。

    另有一条时序理由：15 §6.3 的顺序是「台账写入成功后自动触发后验」，写台账那一刻
    后验结果还不存在，台账上若有这两列就必然是后写的 —— 而台账「写入即固化」。
    """
    cols = set(Base.metadata.tables["ledgers"].c.keys())
    assert "verify_result" not in cols
    assert "cross_aisle" not in cols
    assert "updated_at" not in cols  # 台账写入即固化，与 Snapshot 同一手法


# ------------------------------------------------------------------ 4.5 Verification

def test_verify_result_accepts_both_values(session: Session) -> None:
    """`verify_result` 限 PASS / DEVIATION（17 §九⑩，共享枚举）。"""
    job = _job_order(session)
    _verification(session, job)
    _verification(
        session, job, metric_kind="同批跨巷道", actual_value=4.0, threshold_value=3.0,
        verify_result=VerifyResult.DEVIATION,
    )
    session.flush()

    stored = set(session.execute(text("SELECT verify_result FROM verifications")).scalars().all())
    assert stored == {"PASS", "DEVIATION"}


def test_verify_result_rejected_by_db_check(session: Session) -> None:
    """tasks 4.5 的验证动作：非法取值被拒。"""
    job = _job_order(session)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO verifications "
                "(warehouse_id, job_order_id, metric_kind, actual_value, threshold_value, "
                " verify_result, created_at) "
                "VALUES (:w, :j, '同物料跨巷道', 4.0, 5.0, 'MAYBE', :now)"
            ),
            {"w": WAREHOUSE, "j": job.id, "now": NOW},
        )
    session.rollback()


def test_one_row_per_metric_per_job_order(session: Session) -> None:
    """15 §7.1：入库后验有**两条**口径（同物料 ≤5、同批 ≤3）→ 一行一条指标。

    唯一键 =（作业单, 指标）。没有它，一次重复触发的后验会在偏离清单里
    把同一张单报两遍（15 §7.2 的清单是移库的任务来源，重报会让移库单也翻倍）。
    """
    job = _job_order(session)
    _verification(session, job)
    _verification(session, job, metric_kind="同批跨巷道", actual_value=4.0, threshold_value=3.0)

    with pytest.raises(IntegrityError):
        _verification(session, job)
    session.rollback()


def test_actual_value_is_a_float() -> None:
    """实际值 / 阈值用 Float：17 §10.6 的 KPI 卡片里 `same_material_cross_aisle` 是 **4.8**，
    落位准确率是 0.995 —— 整数列会把它们截断，而截断后的值仍「看着合理」。"""
    cols = Base.metadata.tables["verifications"].c
    assert isinstance(cols.actual_value.type, sa.Float)
    assert isinstance(cols.threshold_value.type, sa.Float)


def test_metric_kind_has_no_enum(session: Session) -> None:
    """后验指标**刻意不建枚举**：D2 列的四类局部值域不含它（17 §4.4 只写「后验指标」），
    而 §9 的 11 个共享枚举也没有它 —— 故按文本存，取值即 15 §7.1 的口径名。

    这是一处**已知的口径缺口**（错拼无法拦截，只会多出一行指标），登记在 tasks.md 9.4c。
    """
    assert "metric_kind" in Base.metadata.tables["verifications"].c
    assert not any(
        name.startswith("ck_verifications_metric_kind") for name in
        {c.name for c in Base.metadata.tables["verifications"].constraints if c.name}
    )


# ------------------------------------------------------------------ 4.6 Deviation

def test_deviation_local_value_domains_match_the_doc() -> None:
    """状态与成因分类是**实体局部值域**（D2 / spec「枚举登记范围与取值」），
    取值照 17 §4.4 原文，且定义在实体模块内。"""
    assert {s.value for s in DeviationStatus} == {"未处理", "已发起移库", "已改善"}
    assert {c.value for c in DeviationCauseKind} == {"新入库收拢不达标", "历史库存拖累"}
    assert DeviationStatus.__module__ == "app.models.job"
    assert DeviationCauseKind.__module__ == "app.models.job"


def test_deviation_local_domains_are_not_in_the_shared_registry() -> None:
    """spec 场景「局部值域不进入共享登记表」——`enums.py` 是**设计文档的逐条搬运**，
    往里加一个实体自己的值域，会让「11 个枚举」这个被三处引用的口径当场失真。"""
    shared_values = {
        member.value
        for enum_cls in vars(shared_enums).values()
        if isinstance(enum_cls, type) and issubclass(enum_cls, shared_enums.Enum)
        for member in enum_cls
    }
    local_values = {
        member.value
        for enum_cls in (PlanKind, DeviationStatus, DeviationCauseKind)
        for member in enum_cls
    }

    assert local_values - shared_values == local_values  # 一个都没混进去
    assert "已改善" not in shared_values
    assert "顺路取" not in shared_values


def test_deviation_status_rejected_by_db_check(session: Session) -> None:
    """局部值域也要落 DB 的 CHECK（D3），否则绕过 ORM 的写入会带进脏状态。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO deviations "
                "(warehouse_id, batch_no, actual_cross_aisle, threshold_cross_aisle, "
                " cause_kind, status, created_at) "
                "VALUES (:w, :b, 6, 3, '新入库收拢不达标', '已忽略', :now)"
            ),
            {"w": WAREHOUSE, "b": BATCH_NO, "now": NOW},
        )
    session.rollback()


def test_deviation_cause_kind_rejected_by_db_check(session: Session) -> None:
    """成因分类决定处置路径（15 §7.2：查引擎/配置 vs 走移库补救）—— 取值必须封闭。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO deviations "
                "(warehouse_id, batch_no, actual_cross_aisle, threshold_cross_aisle, "
                " cause_kind, status, created_at) "
                "VALUES (:w, :b, 6, 3, '说不清', '未处理', :now)"
            ),
            {"w": WAREHOUSE, "b": BATCH_NO, "now": NOW},
        )
    session.rollback()


def test_deviation_needs_at_least_one_identifier(session: Session) -> None:
    """17 §4.4 的标识是「**批次 / 物料**标识」—— 两者至少要有一个。

    两个都空的偏离无法定位到任何货，处置不了也复核不了；这与 D5 给 `CapAlert`
    加「恰好一个来源」CHECK 是同一个理由：拦掉无法处置的脏数据。
    """
    _deviation(session, batch_no=None, material_code=MATERIAL_CODE)  # 只给物料：可以
    session.flush()

    with pytest.raises(IntegrityError):
        _deviation(session, batch_no=None, material_code=None)
    session.rollback()


def test_relocate_started_requires_a_job_order(session: Session) -> None:
    """状态「已发起移库」必须指向那张移库单（26 附录A 的 `Verification → Deviation → JobOrder`
    + 15 §7.2「偏离清单是移库的任务来源」）。

    没有这一列，状态值就只是一个自述：「已发起移库」与「有人改过这格」在库里长得一样。
    """
    with pytest.raises(IntegrityError):
        _deviation(session, status=DeviationStatus.RELOCATE_STARTED, relocate_job_order_id=None)
    session.rollback()

    relocate_job = _job_order(
        session, job_type=JobType.RELOCATE, order_no="MV-88", line_no="1"
    )
    row = _deviation(
        session,
        status=DeviationStatus.RELOCATE_STARTED,
        relocate_job_order_id=relocate_job.id,
    )
    session.flush()
    assert row.relocate_job_order_id == relocate_job.id


def test_relocate_job_order_reference_is_enforced(session: Session) -> None:
    """外键真的生效：指向不存在的作业单被拒（`foreign_keys=ON`，D12）。"""
    with pytest.raises(IntegrityError):
        _deviation(session, relocate_job_order_id=999_999)
    session.rollback()


def test_deviation_keeps_the_threshold_it_was_judged_against(session: Session) -> None:
    """阈值随偏离一并存下（17 §4.4「阈值」）。

    阈值是可配的（15 §10.6 的同批跨巷道 ≤3），若只存实测值，配置一改，
    历史偏离记录就会被按新阈值重新解读 —— 那正是「历史台账不许改写」要防的失真。
    """
    row = _deviation(session)
    assert (row.actual_cross_aisle, row.threshold_cross_aisle) == (6, 3)


def test_payload_json_is_stored_canonically(session: Session) -> None:
    """JSON 列的存储形态唯一：键有序、中文不转义（D4 的引擎级序列化器）。

    形态不唯一时，「同一份推荐理由」在库里会有多种字节表示，比对与去重都不成立；
    中文被转义成 `\\uXXXX` 则让手工排查（`sqlite3` 里直接看）变成解码作业。
    """
    plan = _plan(session, _job_order(session), payload_json={"物料": "PET500 茉莉柚茶", "b": 1, "a": 2})
    session.flush()

    raw = session.execute(
        text("SELECT payload_json FROM recommendation_plans WHERE id = :i"), {"i": plan.id}
    ).scalar_one()

    assert isinstance(raw, str)  # 原生读回是字符串，反序列化在类型上（26 号已提示）
    assert raw == json.dumps({"物料": "PET500 茉莉柚茶", "b": 1, "a": 2},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert "\\u" not in raw


# ------------------------------------------------------------------ §5 销账：指向账号的两条外键

def test_ledger_operator_must_be_an_existing_account(session: Session) -> None:
    """`ledgers.operator_id` 的外键（tasks 9.4d① 销账）。

    §4 落地台账时账号表尚未建模 —— 目标表不在 `Base.metadata` 里，`ForeignKey` 一声明
    就在编译 DDL 时抛 `NoReferencedTableError`，整组测试与建表都跑不起来。故当时按
    「必填整数、无外键」落列，把这条外键记在 9.4d① 由 §5 补。

    「谁执行的」是台账的必要信息（15 §7.3 追溯链），指向一个不存在的账号等于没有来源。
    """
    job = _job_order(session)
    account = _account(session)

    with pytest.raises(IntegrityError) as excinfo:
        _ledger(session, job, operator_id=account.id + 999)

    assert "FOREIGN KEY" in str(excinfo.value).upper()
    session.rollback()


def test_job_order_confirmer_must_be_an_existing_account(session: Session) -> None:
    """`job_orders.confirmed_by_id` 的外键（tasks 9.4d① 销账）。

    确认人是二次确认卡的责任人（15 §7.2「未确认不产生台账」）—— 单据上写着「已确认」
    却指不到人，这条红线就查不下去。同样由 §5 补上外键。
    """
    job = _job_order(session, status=JobStatus.CONFIRMED, disposition=Disposition.ACCEPT)

    with pytest.raises(IntegrityError) as excinfo:
        job.confirmed_by_id = 999_999
        session.flush()

    assert "FOREIGN KEY" in str(excinfo.value).upper()
    session.rollback()


def test_confirmer_points_at_the_account_table() -> None:
    """两条外键的目标都是 `accounts.id`（同一断言的模型侧，迁移侧由空 diff 守卫看住）。"""
    for table, column in (("job_orders", "confirmed_by_id"), ("ledgers", "operator_id")):
        targets = {
            fk.target_fullname
            for fk in Base.metadata.tables[table].c[column].foreign_keys
        }
        assert targets == {"accounts.id"}, f"{table}.{column} 指向了意外的表：{targets}"
