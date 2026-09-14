"""evals 层的共享家当：内存库 session + Golden 加载 + 4 角色 API client。

复用三处既有形态（design.md D7，不另起一套）：
- 内存库 `session`（同 `tests/conftest.py`：StaticPool + 外键 ON + 显式 BEGIN + 保存点回滚）
- `make_scenario` / Spec 类（re-export 自 `tests/logic/conftest.py`，不复制）
- API 三件套（同 `tests/api/conftest.py` 的 `job_api`，但扩到 **4 角色** token，
  供 L2 权限守卫场景 `golden_019/020/021/022` 用）

命名 `eval_api` 而不是 `api` / `job_api`：避免与 tests 里的同名夹具在语义上混淆。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  确保 Base.metadata 完整（建表依赖）
from app.core.config import settings
from app.core.db import json_serializer
from app.core.enums import AccountStatus, Role
from app.core.security import create_session_token
from app.main import create_app
from app.models.base import Base
from app.models.identity import Account
from app.models.job import Deviation, JobOrder, Ledger, Verification
from tests.logic.conftest import (  # noqa: F401  re-export 给 L1/L3 测试用
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
    Scenario,
    make_scenario,
)

#: Golden 数据目录（真相唯一来源 = 13 维度 JSON，见 seed_golden.py）。
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
SCHEMA_FILE = "schema.json"


# ---------------------------------------------------------------------------
# Golden 加载
# ---------------------------------------------------------------------------


def load_golden() -> dict[str, dict]:
    """读 13 维度 JSON，返回 `{golden_NNN: sample}` 的 60 场景目录。"""
    samples: dict[str, dict] = {}
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        if path.name == SCHEMA_FILE:
            continue
        for sample in json.loads(path.read_text(encoding="utf-8")):
            samples[sample["id"]] = sample
    return samples


@pytest.fixture(scope="session")
def golden() -> dict[str, dict]:
    return load_golden()


# ---------------------------------------------------------------------------
# 内存库 session（L1/L3 纯函数测试用）
# ---------------------------------------------------------------------------


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _disable_pysqlite_implicit_begin(dbapi_connection, connection_record) -> None:
    dbapi_connection.isolation_level = None


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
        json_serializer=json_serializer,
        json_deserializer=json.loads,
    )
    event.listen(eng, "connect", _enable_foreign_keys)
    event.listen(eng, "connect", _disable_pysqlite_implicit_begin)

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
    """同 tests/conftest.py 的 session：外层真事务 + 保存点，用例间互不污染。"""
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


# ---------------------------------------------------------------------------
# 4 角色 API client（L2 集成测试用）
# ---------------------------------------------------------------------------


#: 四角色（`13` §2.2）：仓管员 / 计划员 / 主管 / 管理员。
ROLES = (Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR, Role.ADMIN)


@dataclass
class EvalApi:
    """一套独立库 + 覆盖的应用 + 4 角色凭据 + 造数/读库助手。"""

    client: TestClient
    factory: sessionmaker
    tokens: dict[Role, str]

    @property
    def token(self) -> str:
        """默认凭据 = 仓管员（大多数作业端点的合法角色）。"""
        return self.tokens[Role.WAREHOUSE_KEEPER]

    def headers(self, role: Role | None = None) -> dict[str, str]:
        tok = self.tokens[role] if role else self.token
        return {"Authorization": f"Bearer {tok}"}

    # ------------------------------------------------------------ 造数
    def seed(self, **kwargs) -> Scenario:
        with self.factory() as session:
            scenario = make_scenario(session, **kwargs)
            session.commit()
        return scenario

    # ------------------------------------------------------------ 读库
    def orders(self) -> tuple[JobOrder, ...]:
        with self.factory() as session:
            return tuple(session.scalars(select(JobOrder).order_by(JobOrder.id)))

    def ledgers(self) -> tuple[Ledger, ...]:
        with self.factory() as session:
            return tuple(session.scalars(select(Ledger).order_by(Ledger.id)))

    def verifications(self) -> tuple[Verification, ...]:
        with self.factory() as session:
            return tuple(session.scalars(select(Verification).order_by(Verification.id)))

    def deviations(self) -> tuple[Deviation, ...]:
        with self.factory() as session:
            return tuple(session.scalars(select(Deviation).order_by(Deviation.id)))


@pytest.fixture
def eval_api() -> Iterator[EvalApi]:
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
        json_serializer=json_serializer,
        json_deserializer=json.loads,
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    application = create_app()
    application.state.session_factory = factory

    account_ids: dict[Role, int] = {}
    with factory() as session:
        for role in ROLES:
            account = Account(
                warehouse_id=settings.warehouse_code,
                username=f"eval_{role.value}",
                password_hash="eval 用例不验口令，验的是中间件之后的端点行为",
                role=role,
                status=AccountStatus.ACTIVE,
            )
            session.add(account)
            session.flush()
            account_ids[role] = account.id
        session.commit()

    tokens = {
        role: create_session_token(account_ids[role], role.value, AccountStatus.ACTIVE.value)
        for role in ROLES
    }

    try:
        yield EvalApi(client=TestClient(application), factory=factory, tokens=tokens)
    finally:
        engine.dispose()
