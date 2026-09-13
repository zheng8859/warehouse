"""ImportSession 的乐观锁并发守卫（tasks.md 1.2 的验证）。

事实来源：spec `data-model`「乐观锁并发守卫」（Scenario：并发确认仅一方成功）
          16-数据衔接与 cap 自维护 §10.6（乐观锁版本号防止多端同时导入同一时点）
          CLAUDE.md §四（写并发由 ImportSession.lock_version 乐观锁保证）
          app/core/concurrency.py（版本推进的唯一定义处）

## 与 `test_optimistic_lock.py` 的分野

那边用轻量替身钉「比较 + 报错」纯逻辑；这边用**真 `ImportSession` + 真乐观锁辅助**
钉「导入会话这条链真的在推进版本、真的在拒后到写入」。真并发（跨连接可见性）由
文件库用例覆盖 —— 内存库（`StaticPool`）只有一条连接，测不出「先到者 commit 之后、
另一连接读到的版本号变了」这个事实。

## 要钉住的口径

1. **先提交者推进版本**：`lock_version` 0→1（「推进版本」是 spec 原话）。
2. **后提交者被拒**：两端都读到 `lock_version=0`，先到者推进到 1，后到者仍拿 0 提交
   → `StateConflict`（409），`detail` 带 `expected` / `actual` / `entity=ImportSession`。
3. **仅一方成功**：版本只推进一次，不留半成品（被拒时不改实例）。
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表依赖）
from app.core.concurrency import bump_lock_version
from app.core.errors import StateConflict
from app.models.base import Base
from app.models.linkage import ImportSession

pytestmark = pytest.mark.logic

WAREHOUSE = "GTJ10036"
NOW = datetime(2026, 9, 8, 0, 0)


def _new_session() -> ImportSession:
    """造一个 `lock_version = 0` 的导入会话实例（不 add、不 flush —— 交给用例）。"""
    return ImportSession(
        warehouse_id=WAREHOUSE,
        session_no="IMP-20260908-01",
        import_batch_no="BAT-20260908-01",
        data_time=NOW,
    )


# ------------------------------------------------------------------ 单会话：推进 + 拒绝

def test_bump_advances_lock_version(session: Session) -> None:
    """先提交者推进版本：0→1（spec「先提交者成功并推进版本」）。"""
    row = _new_session()
    session.add(row)
    session.flush()
    assert row.lock_version == 0

    assert bump_lock_version(row, expected=0) == 1
    assert row.lock_version == 1


def test_stale_version_is_rejected_with_import_session_entity(session: Session) -> None:
    """后提交者被拒：两端都读 `lock_version=0`，先到者推进到 1，后到者拿 0 提交 → 409。

    `detail.entity == "ImportSession"` 是这条区别于 `test_optimistic_lock.py`（替身
    `_Locked`）的关键断言 —— 前端据此知道冲突来自导入会话，而非作业单。
    """
    row = _new_session()
    session.add(row)
    session.flush()

    bump_lock_version(row, expected=0)  # 先到者
    assert row.lock_version == 1

    with pytest.raises(StateConflict) as excinfo:
        bump_lock_version(row, expected=0)  # 后到者，仍拿着陈旧版本 0

    assert excinfo.value.detail["expected"] == 0
    assert excinfo.value.detail["actual"] == 1
    assert excinfo.value.detail["entity"] == "ImportSession"


def test_rejection_does_not_move_version(session: Session) -> None:
    """被拒时必须纯粹「不写」—— 版本停在先到者推进后的值。"""
    row = _new_session()
    session.add(row)
    session.flush()

    bump_lock_version(row, expected=0)
    with pytest.raises(StateConflict):
        bump_lock_version(row, expected=0)
    assert row.lock_version == 1


# ------------------------------------------------------------------ 文件库：跨连接可见

@pytest.fixture
def file_factory(tmp_path) -> Iterator[sessionmaker]:
    """文件库 + 默认连接池：多连接，测「commit 之后另一连接读到推进后的版本」。

    与 `tests/conftest.py` 的 `session` 夹具（内存 `StaticPool`，单连接）相对 —— 单连接
    里「另一连接读到的版本」这个事实不存在。`ImportSession` 无外键，故不设
    `foreign_keys=ON`（那条 PRAGMA 属有外键的场景，见 `test_confirm_concurrency.py`）。
    """
    engine: Engine = create_engine(
        f"sqlite:///{tmp_path / 'import_concurrency.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def test_concurrent_import_across_connections_only_one_succeeds(
    file_factory: sessionmaker,
) -> None:
    """真并发（两连接）：端 1 推进版本并 commit 后，端 2 读到新版本，拿旧版本提交被拒。

    「仅一方成功」的判据：库里 `lock_version` 最终为 1（不是 2），端 2 的陈旧版本在
    `bump_lock_version` 处被拒（`detail.actual == 1` 而非 0）。
    """
    with file_factory() as seed:
        seed.add(_new_session())
        seed.commit()

    with file_factory() as conn1:
        row1 = conn1.scalars(select(ImportSession)).one()
        assert row1.lock_version == 0
        bump_lock_version(row1, expected=0)
        conn1.commit()

    with file_factory() as conn2:
        row2 = conn2.scalars(select(ImportSession)).one()
        assert row2.lock_version == 1, "端 1 的推进已 commit，端 2 应读到 1"
        with pytest.raises(StateConflict) as excinfo:
            bump_lock_version(row2, expected=0)  # 陈旧版本
        assert excinfo.value.detail["actual"] == 1
        assert excinfo.value.detail["expected"] == 0

    with file_factory() as check:
        final = check.scalars(select(ImportSession)).one()
        assert final.lock_version == 1, "版本只应推进一次"
