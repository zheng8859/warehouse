"""tests/api 的共享家当：`job_api` 夹具 —— 独立库 + 覆盖的应用 + 会话凭据 + 造数助手。

复用 `test_allocate.py` 同一套「引擎 + 工厂 + 应用」三件套，理由见那个文件的模块
docstring（`session` 夹具把用例包在外层事务里，与端点自开会话撞车；故每个用例一套
独立的库 + `application.state.session_factory` 覆盖）。只收**阶段四作业端点**
（confirm / reject / retry / void / ledger / verification）需要的读库助手；
`allocate` 那套（`plan_rows` / `raw_payload_text`）留在 `test_allocate.py` 自己身上，
不搬进这个共享文件。

命名 `job_api` 而不是 `api`：`test_allocate.py` / `test_auth.py` 各自已有同名的本地
夹具，这里不与之抢名 —— 夹具名撞车时本地覆盖 conftest，会让「谁是谁」靠读 diff 猜。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表依赖）
from app.core.config import settings
from app.core.db import json_serializer
from app.core.enums import AccountStatus, Role
from app.core.security import create_session_token
from app.main import create_app
from app.models.base import Base
from app.models.identity import Account
from app.models.job import Deviation, JobOrder, Ledger, Verification
from tests.logic.conftest import Scenario, make_scenario

#: 造数用的仓库号（`17` §十一：首期固定 GTJ10036）。
WAREHOUSE_ID = "GTJ10036"


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """与 conftest 同一处置：SQLite 默认关闭外键，不显式打开则 FK 形同虚设。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


@dataclass
class Api:
    """一个用例的全套家当：被覆盖的应用、它背后的库、以及一个可用的会话凭据。"""

    client: TestClient
    factory: sessionmaker
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ------------------------------------------------------------ 造数
    def seed(self, **kwargs) -> Scenario:
        """建一批数据并**提交**（端点用的是另一个会话，只 flush 的行它看不见）。"""
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
def job_api() -> Iterator[Api]:
    """独立的「引擎 + 工厂 + 应用」三件套（见模块 docstring）。"""
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
    # 中间件与 deps.get_db 都读这个属性 —— 一处覆盖，两处生效。
    application.state.session_factory = factory

    with factory() as session:
        account = Account(
            warehouse_id=settings.warehouse_code,
            username="gtj_keeper",
            # 不走登录端点，凭据直接签发（同 test_allocate.py：省掉每条用例一次 bcrypt）。
            password_hash="端点用例不验口令，验的是中间件之后的端点行为",
            role=Role.WAREHOUSE_KEEPER,
            status=AccountStatus.ACTIVE,
        )
        session.add(account)
        session.commit()
        account_id = account.id

    token = create_session_token(
        account_id, Role.WAREHOUSE_KEEPER.value, AccountStatus.ACTIVE.value
    )

    try:
        yield Api(client=TestClient(application), factory=factory, token=token)
    finally:
        engine.dispose()
