"""数据库引擎与会话。

事实来源：19-系统架构与部署视图 §3.3（单库、WAL 模式）；选型确认 SQLite。

SQLite 的三个 PRAGMA 都是必须的，缺一不可：
  - `journal_mode=WAL`   多读单写，避免读阻塞写
  - `foreign_keys=ON`    SQLite 默认**关闭**外键约束，不显式打开则 FK 形同虚设
  - `busy_timeout`       WAL 下写冲突时等待而非立刻报 database is locked

并发约束（openspec/config.yaml 技术栈节）：应用**必须单进程运行**，
不得以多 worker 并发写。写并发由 JobOrder / ImportSession 的乐观锁版本号保证，
busy_timeout 仅作兜底。

「cap 与台账同事务写入 + 事务整体回滚」（16 §6.3）必须由真实事务保证 ——
不得用应用层补偿代替，否则会破坏「台账是 cap 增量唯一来源」这个不变量。
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings


def _configure_sqlite_connection(
    dbapi_connection: Any, connection_record: Any
) -> None:
    """在每个 DBAPI 连接建立时设置 PRAGMA。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=%d" % settings.sqlite_busy_timeout_ms)
        # WAL 下 NORMAL 已能保证崩溃不丢已提交事务，兼顾吞吐。
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=settings.sqlalchemy_echo,
    future=True,
    # check_same_thread=False 是 FastAPI 线程池 + SQLite 的常规要求；
    # 安全性由「单进程 + 乐观锁」约束共同保证，不要借此开启多 worker 写。
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

if _is_sqlite:
    event.listen(engine, "connect", _configure_sqlite_connection)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    # 允许 commit 后继续读取已加载对象（API 层返回响应用），
    # 不影响事务语义：cap 与台账仍在同一事务内提交。
    expire_on_commit=False,
)


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话，异常时回滚，结束时关闭。"""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
