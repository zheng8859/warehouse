"""确认的乐观锁并发守卫（tasks.md 7.3 的验证）。

事实来源：openspec/specs/data-model/spec.md「乐观锁并发守卫」（Scenario：并发确认仅一方成功）
          15-入库出库移库与后验流程设计 §10.6（防止多端同时确认同一作业单）
          CLAUDE.md §四（写并发由 JobOrder.lock_version 乐观锁保证）
          app/core/concurrency.py（版本推进的唯一定义处）

## 要钉住的口径

1. **先提交者推进版本**：确认成功 `lock_version` 0→1（「推进版本」是 spec 原话）。
2. **后提交者被拒**：两端都读到 `lock_version=0`，先到者确认推进到 1，后到者仍拿着 0
   提交 → `StateConflict`（409），`detail` 带 `expected` / `actual` 供前端「请重新读取」。
3. **仅一方成功**：被拒的一方不写第二条台账 —— 一条 `JobOrder` 至多一正常台账行。

与 `test_optimistic_lock.py` 的分野：那边用轻量替身钉「比较 + 报错」纯逻辑；这边用
**真 `JobOrder` + 真确认编排**钉「确认这条链真的在推进版本、真的在拒后到写入」。真并发
（跨连接可见性）由文件库用例覆盖 —— 内存库（`StaticPool`）只有一条连接，测不出
「先到者 commit 之后、另一连接读到的版本号变了」这个事实。
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表依赖）
from app.core.enums import AccountStatus, JobStatus, JobType, Role
from app.core.errors import StateConflict
from app.models.base import Base
from app.models.identity import Account
from app.models.job import JobOrder, Ledger
from app.models.linkage import Snapshot
from app.services.inbound import confirm_inbound

from .conftest import JobOrderSpec, make_scenario

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 8, 0, 0)
BATCH = "GJP2571221"
MATERIAL = "M1"


def _operator(session: Session) -> Account:
    account = Account(
        warehouse_id=WAREHOUSE,
        username="gtj_keeper",
        password_hash="$2b$12$" + "0" * 53,
        role=Role.WAREHOUSE_KEEPER,
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    session.flush()
    return account


def _planned_inbound(session: Session) -> tuple[JobOrder, Snapshot, int]:
    """造一张 `PLANNED` 入库单 + 默认快照 + 操作员，返回 (单, 快照, 操作员 id)。"""
    operator = _operator(session)
    scenario = make_scenario(
        session,
        job_orders=[
            JobOrderSpec(
                order_no="PO-01",
                material_code=MATERIAL,
                qty=40,
                batch_no=BATCH,
                job_type=JobType.INBOUND,
                status=JobStatus.PLANNED,
            )
        ],
    )
    session.flush()
    return scenario.job_orders[0], scenario.snapshot, operator.id


# ------------------------------------------------------------------ 单会话：推进 + 拒绝

def test_confirm_advances_lock_version(session: Session) -> None:
    """先提交者推进版本：确认成功 `lock_version` 0→1（spec「先提交者成功并推进版本」）。"""
    order, snapshot, operator_id = _planned_inbound(session)
    assert order.lock_version == 0

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator_id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=snapshot,
        lock_version=0,
    )

    assert order.status is JobStatus.VERIFIED
    assert order.lock_version == 1


def test_stale_version_confirm_is_rejected(session: Session) -> None:
    """后提交者被拒：两端都读 `lock_version=0`，先到者推进到 1，后到者拿 0 提交 → 409。

    `detail` 带 `expected` / `actual` —— 前端据此渲染「请重新读取」，而不是把冲突当成
    自己的 bug。被拒的一方不写第二条台账（「仅一方成功」）。
    """
    order, snapshot, operator_id = _planned_inbound(session)
    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator_id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=snapshot,
        lock_version=0,
    )
    assert order.lock_version == 1

    with pytest.raises(StateConflict) as excinfo:
        confirm_inbound(
            session,
            job_order=order,
            operator_id=operator_id,
            executed_at=NOW,
            target_location_code="010105",
            snapshot=snapshot,
            lock_version=0,  # 后到者仍拿着陈旧版本
        )

    assert excinfo.value.detail["expected"] == 0
    assert excinfo.value.detail["actual"] == 1
    assert excinfo.value.detail["entity"] == "JobOrder"

    ledgers = session.scalars(select(Ledger)).all()
    assert len(ledgers) == 1, "被拒的一方不得写第二条台账"


def test_confirm_without_version_bumps_without_validating(session: Session) -> None:
    """`lock_version=None`（未接版本回环的调用方）仍推进版本、不校验 —— 与 `allocate`
    的 `bump_lock_version(order, expected=order.lock_version)` 同一口径。"""
    order, snapshot, operator_id = _planned_inbound(session)

    confirm_inbound(
        session,
        job_order=order,
        operator_id=operator_id,
        executed_at=NOW,
        target_location_code="010104",
        snapshot=snapshot,
        # 不传 lock_version
    )

    assert order.status is JobStatus.VERIFIED
    assert order.lock_version == 1


# ------------------------------------------------------------------ 文件库：跨连接可见

def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


@pytest.fixture
def file_factory(tmp_path) -> Iterator[sessionmaker]:
    """文件库 + 默认连接池：多连接，测「commit 之后另一连接读到推进后的版本」。

    与 `tests/conftest.py` 的 `session` 夹具（内存 `StaticPool`，单连接）相对 —— 单连接
    里「另一连接读到的版本」这个事实不存在。不用 WAL：这里测的是乐观锁的版本推进与
    拒绝，不是文件锁与跨连接事务的原子性（那属 tasks 9.4 的「cap 与台账同事务」）。
    """
    engine: Engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def test_concurrent_confirm_across_connections_only_one_succeeds(
    file_factory: sessionmaker,
) -> None:
    """真并发（两连接）：端 1 确认 commit 后，端 2 读到推进后的版本，拿旧版本提交被拒。

    「仅一方成功」的判据分三段：库里只有一张台账、`VERIFIED` 只由端 1 产生、
    端 2 的陈旧版本在 `bump_lock_version` 处被拒（`detail` 的 `actual` 是 1 不是 0）。
    """
    # 造数 + commit（锁版本 0 落库）
    with file_factory() as seed:
        order, snapshot, operator_id = _planned_inbound(seed)
        snapshot_id, order_id = snapshot.id, order.id
        seed.commit()

    # 端 1：读到 lock_version=0，确认成功推进到 1，commit。
    with file_factory() as conn1:
        order1 = conn1.scalars(select(JobOrder).where(JobOrder.id == order_id)).one()
        snap1 = conn1.scalars(select(Snapshot).where(Snapshot.id == snapshot_id)).one()
        assert order1.lock_version == 0
        confirm_inbound(
            conn1,
            job_order=order1,
            operator_id=operator_id,
            executed_at=NOW,
            target_location_code="010104",
            snapshot=snap1,
            lock_version=0,
        )
        conn1.commit()

    # 端 2：读到推进后的 lock_version=1，但携带陈旧版本 0 提交 → 被拒（在改任何东西之前）。
    with file_factory() as conn2:
        order2 = conn2.scalars(select(JobOrder).where(JobOrder.id == order_id)).one()
        assert order2.lock_version == 1, "端 1 的推进已 commit，端 2 应读到 1"
        with pytest.raises(StateConflict) as excinfo:
            confirm_inbound(
                conn2,
                job_order=order2,
                operator_id=operator_id,
                executed_at=NOW,
                target_location_code="010105",
                snapshot=None,  # 拒绝发生在乐观锁处，先于任何快照使用
                lock_version=0,
            )
        assert excinfo.value.detail["actual"] == 1
        assert excinfo.value.detail["expected"] == 0

    # 仅一方成功：一张台账、一张 VERIFIED。
    with file_factory() as check:
        ledgers = check.scalars(select(Ledger).where(Ledger.job_order_id == order_id)).all()
        assert len(ledgers) == 1
        final = check.scalars(select(JobOrder).where(JobOrder.id == order_id)).one()
        assert final.status is JobStatus.VERIFIED
        assert final.lock_version == 1
