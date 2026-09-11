"""pytest 共享夹具。

三层 TDD（00-总体开发方案 §3.1）：模型测试 / API 测试 / 逻辑测试。
L1 单元须小于 5 秒（pre-commit 门禁）。

## 测试库形态：内存库（design.md D12）

`26` 号的「基础设施常见问题」表与 design.md D12 都开方**测试用内存库 + `create_all`**：
测试库是瞬时的，**模型即事实来源**，不引入迁移（迁移只服务真实库，见 D8）。

本文件的阶段一骨架注记写的是「用临时文件库并开启 WAL，不要用内存库」，与此冲突。
按 `CLAUDE.md` 的「设计文档是事实来源，代码是它的派生物」以 design.md 为准。

两处必须改掉骨架注记里的写法：

  - **不设 WAL**：内存库不支持 —— `PRAGMA journal_mode=WAL` 在 `:memory:` 上返回
    `"memory"`。测试库只开 `foreign_keys=ON` 这一条 PRAGMA。
  - **必须用 `StaticPool`**：内存库的生命周期绑在**连接**上。默认连接池会为不同会话
    另开连接，各自看到全新的空库，于是 `create_all` 建的表在用例里「不存在」。

**保留的正当担忧**：内存库会掩盖 WAL 的文件锁与跨连接事务行为。本阶段无妨 ——
阶段二的测试是 CRUD、状态机与权限判定。但阶段四的「cap 与台账同事务写入 + 整体回滚」
必须由真实事务证明，那条不变量的测试需要**文件库**，届时在此增设一个文件库夹具
（见 design.md Risks 与 tasks.md 9.4 的登记）。

## 父表夹具

`26` 号提示过：按依赖链顺序建父表记录，否则外键必失败。夹具工厂随各数据链在
此追加（主数据链 → 衔接链 → 作业链 → …），不预先造空壳。
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表与迁移依赖）
from app.core.db import json_serializer
from app.models.base import Base


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite 默认**关闭**外键约束，不显式打开则 FK 形同虚设。

    与 `app/core/db.py` 是同一件事，但刻意不复用那个函数：它会一并设置
    `journal_mode=WAL`，而内存库不支持 WAL（见模块 docstring）。
    测试库只开外键这一条。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _disable_pysqlite_implicit_begin(dbapi_connection, connection_record) -> None:
    """把事务边界交还给 SQLAlchemy —— pysqlite 的隐式事务管理有洞。

    Python 的 `sqlite3` 默认**只在 DML 之前**隐式 `BEGIN`；DDL（`CREATE` / `DROP`）
    直接走 autocommit。于是建表语句逃出事务、回滚不掉，用例之间的隔离就漏了。
    这是 SQLAlchemy 对 pysqlite 的标准处方的一半（另一半是下面的 `begin` 钩子）。
    """
    dbapi_connection.isolation_level = None


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """整个测试会话共用一个内存库；建表一次。"""
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
        # JSON 列的序列化口径与生产**必须一致**：`test_linkage.py` 断言的是存储形态
        # （键有序、中文不转义），两边的序列化器不同就等于在测另一套配置。
        # 这里与 db.py 的 PRAGMA 处理不同 —— 那一处不能复用（它会一并开 WAL），
        # 而序列化口径本就是同一件事，复用才有意义。
        json_serializer=json_serializer,
        json_deserializer=json.loads,
    )
    event.listen(eng, "connect", _enable_foreign_keys)
    event.listen(eng, "connect", _disable_pysqlite_implicit_begin)

    # 配合上面的 isolation_level=None：显式发 BEGIN，让 SQLAlchemy 真正掌控事务，
    # 保存点与整体回滚才成立（见 test_infra.py 的回滚用例）。
    @event.listens_for(eng, "begin")
    def _emit_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """每个用例一个会话，结束时整体回滚 —— 用例之间互不污染。

    外层连接开真事务，会话用 `create_savepoint` 接进去：用例内 `session.commit()`
    提交的是保存点而非真事务，因此既能测「提交后读回」，又不把数据漏给下一个用例。
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def foreign_keys_on(session: Session) -> bool:
    """读回 PRAGMA 的实际值 —— 供用例断言外键真的生效，而非「配置了但没生效」。"""
    return session.execute(text("PRAGMA foreign_keys")).scalar() == 1
