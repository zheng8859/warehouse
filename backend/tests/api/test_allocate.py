"""批量分配端点 `POST /api/allocate/batch` 的契约测试（`tasks.md` 8.1 的验证条件）。

事实来源：`17` §10.7（请求 / 响应 / 字段表）、§4.1（作业单标识）、§4.2（方案实体）、
          `14` §3.4（批量竞争分配）、§3.5（A 类降级告警）
          `design.md` D3（配置缺席的两种处置）、D4（扣减只落内存）、D8（三处降级不混用）、
          D9 第 3 条（列与 JSON 的同名字段是同一次写入的投影）、D10（端点契约）、
          D11（批次号）、D12（失败与恢复的六行处置表，尤其是「写库异常 ⇒ 整批整体回滚」那行）、
          D16（`aisles` 单条 / 告警三字段 / `snapshot_version` 的选型）、
          D17（业务钟）
          spec `recommendation-engine`「批量分配端点契约」「因子级降级与两种降级不得混用」
            「排序降级」「四级走尽则该单失败而非静默落位」「A 类降级触发告警」
          `13` §2.2（权限矩阵）、§6.1（三层检查的第 1、2 层）
          spec `auth`「无状态会话凭据」、spec `permission`「访问边界与免认证白名单」
          `tasks.md` 8.1 / 9.1 / 9.4（本文件的任务书；9.x 的两节为何也落在 API 层，见文末）

## 本文件覆盖 8.1 ~ 8.5 与 9.1 / 9.4

**8.4（认证与权限）与前三节的关系**：401 由既有中间件返回；端点级资源鉴权**已对
`inbound.operate` 生效**（`POST /api/allocate/batch` 挂 `require_permission(inbound.operate)`，
仅仓管员/管理员 200，计划员/主管 403，见 8.4 的用例）。留在这里而不是新开一个文件，
理由与 8.2/8.3 相同：它们都要一套「引擎 + 工厂 + 应用」，而端点行为是同一份契约的不同
侧面。**那条「谁被 403」的用例**钉的是「端点挂了 `inbound.operate` 这个检查」这件事本身
—— 写在这里意味着下一个人摘掉或改错 checker 时，红的是本文件里一条有说明的用例，
而不是某个前端页面。

**8.1 那一节**（端点的**存在**、显式集合驱动、响应次序）对「已提交的那两条单」只断言
产出方案、**不断言它们的 `status`** —— 当时停在 `PLANNED` 是 8.2 的事，8.1 的实现不该
顺手把状态改了。**8.2 落地后这条自我约束不必撤销**：8.1 的用例原样保留，状态与批次号的
断言集中在文末「8.2 状态迁移与批次号回写」一节。分节而不是就地改写，是为了让「哪条用例
守的是哪个任务书」始终读得出来 —— 混在一起之后，一行 `assert order.status is ...` 到底
是 8.1 的还是 8.2 的，就只能靠读 diff 猜。

## 复用 `tests/logic/` 的造数而不是在这里再造一份

`tests/logic/conftest.py` 的模块 docstring 明写「同一份造数因此既能给 `tests/logic/` 用，
也能给 `tests/api/` 用」。本文件按那条约定直接引用它，两处因此共用**同一组**默认权重 /
快照时点 / 造数形状：换一份造数就会让「逻辑层验过的形态」与「端点层验的形态」悄悄分叉。

有一处必须自己补：`make_scenario` 只 `flush()`，不 `commit()`。端点走的是**另一个**会话
（`deps.get_db` 从 `app.state.session_factory` 开），只 flush 的行它看不见 —— 表象是
「造了数却报未知单号」。故本文件的 `seed()` 提交。

## 为什么自带一套「引擎 + 工厂 + 应用」而不是用 `tests/conftest.py` 的 `session` 夹具

与 `tests/api/test_auth.py` 同一处置，理由见那里的模块 docstring：`session` 夹具把用例
包在一层外层事务里，而端点在请求内自己开会话；内存库是 `StaticPool`（全进程一条连接），
「外层已 BEGIN，请求内又要 BEGIN」必然撞车。故每个用例一套独立的库 +
`application.state.session_factory` 覆盖（中间件与 `deps.get_db` 读的是同一个属性）。

与 `test_auth.py` 的两处不同：

1. **建全部 23 张表**（`test_auth.py` 只建 `accounts` 一张，为的是省 pre-commit 的 L1 预算）。
   本端点要读 8 张表，省不掉；实测 `create_all` 约 12 ms，每个用例一套库仍然跑得动。
2. **引擎带 `json_serializer`**（与 `app/core/db.py` 那一份同一函数）。不带上它，
   「库里那串文本长什么样」就不是生产的样子，而 D4 对存储形态有明确要求（非转义 + 键序
   固定）—— 下面 `test_...payload_is_the_written_payload...` 直接读列文本断言这件事。

## 8.1 期间定下、需要落到 `design.md` 的口径（本文件按此断言）

- **`job_order_ids` 的线上形态 = `str(JobOrder.id)`**（十进制、无前导零）。实现按
  `^[1-9]\\d*$` 严格解析：放任 `"007"` 与 `"7"` 并存，`order_queue` 的按号索引会把
  同一行当成两条队列项，而**重号在分配器里是 `ValueError`、在这里是静默少一张单**。
- **当前快照 = 本仓 `version_no` 最大者**（`Snapshot` 不是配置版本，没有 `effective_at`，
  故不复用 `config_version.pick_current_version`；`version_no` 的列注释即「按仓库单调递增」）。
- **未提交的单：状态与方案均不变**（8.1 的验证条件原文）。
- **无 `Snapshot` 行 ⇒ 阻断（409）**：响应契约的 `snapshot_version` 无值可渲染，且无 cap
  可读。9.5 要的「入库无快照不阻断」是**引擎级**的行为（`allocate_batch` 吃
  `SnapshotIndex.absent`），与这里的端点级关口不冲突 —— 两处在 `design.md` 一并登记。

## 9.1 与 9.4 也落在这里

两节验的都是**端点在异常路径上的行为**：权重缺席时的整批阻断（9.1）与写库异常时的整批
回滚（9.4）。任务书对 9.1 只写了「整批阻断」、对 9.4 写的是 `tests/logic/`，但被验的东西
（配置缺席的处置、事务边界）**整个在端点与依赖里** —— `app/engine/` 按 D4 从不碰会话，
而 `tests/logic/` 的 `session` 夹具看不见真提交（端点里的 `commit()` 只释放外层事务里的
一个 savepoint），在那一层写「整批回滚」会得到一份**永远绿**的假保证。故 9.4 按落点现实
调整到本文件，理由与 8.4 那次同类（8.4 是纯验证性的，9.4 不是：产品代码在 8.2 那一步就
已经写对了，本节的用例把它钉住）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  必须导入：确保 Base.metadata 完整（建表依赖）
from app.core.config import settings
from app.core.db import json_serializer
from app.core.enums import AccountStatus, JobStatus, Role
from app.core.security import create_session_token
from app.api.permissions import Permission, check
from app.main import create_app
from app.models.base import Base
from app.models.identity import Account
from app.models.job import JobOrder, Ledger, PlanKind, RecommendationPlan
from app.models.linkage import AisleCap
from app.schemas.reason import FACTOR_NAMES, MAX_JOB_ORDERS_PER_BATCH, ReasonPayload
from tests.logic.conftest import (
    DEFAULT_SNAPSHOT_TIME,
    DEFAULT_WAREHOUSE_ID,
    DEFAULT_WEIGHTS,
    AisleSpec,
    InventorySpec,
    JobOrderSpec,
    MaterialSpec,
    Scenario,
    make_scenario,
)

ALLOCATE_URL = "/api/allocate/batch"

#: 造数用的仓库号（`17` §十一：首期固定 GTJ10036）。
WAREHOUSE_ID = DEFAULT_WAREHOUSE_ID

#: 报文里的 `snapshot_version` = 造数快照时点的 `"%Y-%m-%dT%H:%M"` 渲染（D10）。
#: 直接从 `DEFAULT_SNAPSHOT_TIME` 推，而不是写死 `"2026-09-08T00:00"`：造数改为别的时点时，
#: 这条断言会跟着走，而不是变成一处「用例红、代码对」的噪音。
SNAPSHOT_VERSION = DEFAULT_SNAPSHOT_TIME.strftime("%Y-%m-%dT%H:%M")

#: 批次号形状（D11）。只钉形状与「当日」两件事 —— 序号那一位是 8.2 回写后的账。
_BULK_BATCH_NO_PATTERN = re.compile(r"^BAT-\d{8}-\d{2,}$")

#: 用例里每条单的需量：10 板。与近站台巷道的 `cap_total=10` **刻意相等** ——
#: 第一条单正好吃满近站台，第二条必然被挤到远巷道。这里是 8.1 最省事的造法，
#: 但它不是巧合：`cap_total` 改成 20 时下面「第二条落在 02」的断言会当场红。
QTY_PER_ORDER = 10


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
    """与 conftest 同一处置：SQLite 默认关闭外键，不显式打开则 FK 形同虚设。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


#: 「不给这个键」的哨兵 —— 与显式的 `None` 分得开（见 `Api.allocate`）。
_OMITTED = object()


@dataclass
class Api:
    """一个用例的全套家当：被覆盖的应用、它背后的库、以及一个可用的会话凭据。"""

    client: TestClient
    factory: sessionmaker
    token: str

    # ------------------------------------------------------------ 造数
    def seed(self, **kwargs) -> Scenario:
        """建一批数据并**提交**（端点用的是另一个会话，只 flush 的行它看不见）。"""
        with self.factory() as session:
            scenario = make_scenario(session, **kwargs)
            session.commit()
        return scenario

    # ------------------------------------------------------------ 动作
    def allocate(
        self,
        job_order_ids: Sequence[str | int],
        *,
        warehouse_id: str = WAREHOUSE_ID,
        snapshot_version: object = _OMITTED,
    ):
        """调一次批量分配。`snapshot_version` 不给即**不带这个键**（它是可空的声明）。

        `None` 与「不给」必须分得开：报文里 `snapshot_version` 出现且为 `null` 与整个键
        缺席，是两种不同的请求（前者是显式声明「我不钉快照」）。用哨兵而不是 `None`
        作默认值，就是为了让用例能分别表达这两件事。
        """
        body: dict = {
            "warehouse_id": warehouse_id,
            "job_order_ids": [str(order_id) for order_id in job_order_ids],
        }
        if snapshot_version is not _OMITTED:
            body["snapshot_version"] = snapshot_version
        return self.client.post(
            ALLOCATE_URL,
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    # ------------------------------------------------------------ 读库
    def plan_rows(self) -> tuple[RecommendationPlan, ...]:
        with self.factory() as session:
            return tuple(
                session.scalars(
                    select(RecommendationPlan).order_by(RecommendationPlan.id)
                )
            )

    def orders(self) -> tuple[JobOrder, ...]:
        with self.factory() as session:
            return tuple(session.scalars(select(JobOrder).order_by(JobOrder.id)))

    def raw_payload_text(self, plan_id: int) -> str:
        """读 `payload_json` 那一列的**原始文本**（不经 JSON 类型装饰器反序列化）。

        用裸 `text()` 而不是模型：要断言的就是「库里存的那串字符」，走 ORM 会先被
        `json_deserializer` 还原成 dict，存储形态那件事就验不到了。
        """
        with self.factory() as session:
            return session.execute(
                text("select payload_json from recommendation_plans where id = :plan_id"),
                {"plan_id": plan_id},
            ).scalar_one()


@pytest.fixture
def api() -> Iterator[Api]:
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
            # 本文件不走登录端点（登录由 test_auth.py 覆盖），凭据直接签发 ——
            # 中间件只解码凭据 + 回查状态，从不看口令哈希。故这里放一个字面量占位符，
            # 顺带省掉每条用例一次 bcrypt（成本因子 12 下每次约 0.28s，够把 L1 顶出预算）。
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


# ------------------------------------------------------------------ 造数助手

def order_ids(scenario: Scenario) -> tuple[str, ...]:
    """造出来的作业单的线上标识（= `str(JobOrder.id)`），按建表序。"""
    return tuple(str(order.id) for order in scenario.job_orders)


def aisles_of(body: dict, job_order_id: str) -> list[str]:
    """响应里某条单落到的巷道（`plans[]` 按优先级降序，故按单号取而不是按下标取）。"""
    by_id = {plan["job_order_id"]: plan for plan in body["plans"]}
    return by_id[job_order_id]["aisles"]


def _capacity_scenario(
    api: Api,
    *,
    orders: int = 5,
    specs: Sequence[JobOrderSpec] | None = None,
    **overrides,
) -> Scenario:
    """两条巷道的两档形态：`01` 近站台 10 格、`02` 远巷道 100 格。

    `station_weight` 两档都给：`station` 因子的**唯一**参与前提是有 `AisleStation` 行，
    不给就会多出一条与用例无关的因子级降级，读断言的人得先把它排除掉才知道自己在看什么。

    `specs` 给了就用它、忽略 `orders`：要造「已排过计划的单」「塞不下的单」这类**个别**
    形状时，逐个写 `JobOrderSpec` 比给通用生成器加一堆开关更直白 —— 顺序也有意义
    （同优先级下队列顺序 = 单据号升序，故**先给的先挑巷道**）。
    """
    if specs is None:
        specs = [
            JobOrderSpec(
                order_no=f"PO-20260908-{n:03d}",
                material_code="MOK",
                material_name="茉莉柚茶",
                qty=QTY_PER_ORDER,
                abc_class="A",
                batch_no="B26090801",
            )
            for n in range(1, orders + 1)
        ]
    return api.seed(
        aisles=[
            AisleSpec("01", cap_total=10, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec("MOK", abc_class="A", material_name="茉莉柚茶")],
        job_orders=list(specs),
        **overrides,
    )


def _order_spec(
    order_no: str,
    *,
    qty: int = QTY_PER_ORDER,
    status: JobStatus | str = JobStatus.PENDING,
) -> JobOrderSpec:
    """本文件造单的公共形状：`MOK` / A 类 / 有批号（批号不给会让 `batch` 因子降级）。"""
    return JobOrderSpec(
        order_no=order_no,
        material_code="MOK",
        material_name="茉莉柚茶",
        qty=qty,
        abc_class="A",
        batch_no="B26090801",
        status=status,
    )


# ------------------------------------------------------------------ 夹具自检

def test_the_test_app_really_uses_the_test_database(api: Api) -> None:
    """夹具自检：确认 `app.state.session_factory` 真的被换掉了。

    这一条防的是**最坏的一种绿**：覆盖没生效时，端点会去读写 `app.core.db.SessionLocal`
    —— 那是真实开发库（`data/warehouse.db`）。届时「拿到了方案」这条断言仍可能通过
    （开发库里恰好有作业单与快照），而用例已经不再测被测对象，还会往开发库里写方案行。
    """
    from app.core.db import SessionLocal

    with api.factory() as session:
        assert session.get_bind().url.database in (None, ":memory:"), (
            "测试工厂指向的不是内存库 —— 覆盖没生效"
        )
    assert SessionLocal is not api.factory


# ------------------------------------------------------------------ 8.1 显式集合驱动

def test_only_the_submitted_orders_get_plans(api: Api) -> None:
    """8.1 的核心：「显式集合驱动分配」—— 5 条 PENDING 只提交 2 条。

    验证条件（任务书原文）：其余 3 条**状态与方案均不变**。这一条就是「批量分配不是
    全量重算」在端点上的形态：`job_order_ids` 是必填的显式集合，没有「空即全量」。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)
    assert len(ids) == 5, "造数变了：本用例的前提是 5 条在库单"
    submitted = [ids[0], ids[3]]
    untouched = {ids[1], ids[2], ids[4]}

    body = api.allocate(submitted).json()

    # 只有被提交的那两条出方案
    assert {plan["job_order_id"] for plan in body["plans"]} == set(submitted)

    # 未提交的三条：状态仍是 PENDING、且一行方案都没有（`计划` 与 `状态` 两件事都查，
    # 少查一件就漏掉「只写方案不改状态」或「改了状态却没写方案」这两种半边错）
    for order in api.orders():
        if str(order.id) in untouched:
            assert order.status is JobStatus.PENDING
    assert {row.job_order_id for row in api.plan_rows()} == {int(i) for i in submitted}


def test_the_first_order_takes_the_near_station_aisle_and_the_second_is_pushed_out(api: Api) -> None:
    """容量竞争的端点形态：近站台 10 格，两条 10 板的单 ⇒ 先入队的吃满、后者去远巷道。

    这条用例同时钉住三件在端点层才看得见的事：队列序（同优先级按单据号升序）、
    内存扣减（D4：第一条占的 10 格第二条看得见）、以及降级告警的 `aisle` = **降级后
    实际落到的巷道**（D16），与 `plans[].aisles` 对齐。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)

    body = api.allocate([ids[0], ids[3]]).json()

    by_id = {plan["job_order_id"]: plan for plan in body["plans"]}
    assert by_id[ids[0]]["aisles"] == ["01"], "近站台的空位没给队列里的第一条"
    assert by_id[ids[3]]["aisles"] == ["02"], "近站台吃满后，第二条应落到远巷道"

    alerts = body["degraded_alerts"]
    assert [alert["job_order_id"] for alert in alerts] == [ids[3]]
    assert alerts[0]["aisle"] == "02"
    # 缺口 = 本单需量 10 − 近站台剩余可用 0（`14` §3.5 的 X）。文案照 spec 逐字。
    assert alerts[0]["message"].startswith("近站台缺口 10 板，建议移库腾挪")


def test_a_non_a_class_order_pushed_out_raises_no_alert(api: Api) -> None:
    """告警是 **A 类专属**（`14` §3.5 / `is_alarming`）：C 类降到远巷道是正常分配，不是事故。

    造法：近站台巷道 `cap_total=0`（有这一行、容量为零），C 类单在档 0 与档 1 都无可行
    容量 ⇒ 落到远巷道、`stop_tier=2`。**方案级降级照常发生**（`degraded` 真、理由在），
    但 `degraded_alerts` 必须是空的 —— 这两件事分开正是 D8 要的：
    「走了降级链」与「该报警」不是同一个判据。
    """
    scenario = api.seed(
        aisles=[
            AisleSpec("01", cap_total=0, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec("MOK", abc_class="C", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-20260908-001",
                material_code="MOK",
                qty=QTY_PER_ORDER,
                abc_class="C",
                batch_no="B26090801",
            )
        ],
    )
    ids = order_ids(scenario)

    body = api.allocate(ids).json()

    assert aisles_of(body, ids[0]) == ["02"]
    assert body["degraded_alerts"] == []

    (row,) = api.plan_rows()
    assert row.degraded is True, "走了降级链（停在第 2 档）就必须标降级"
    assert row.degrade_reason, "降级不静默：方案级降级必须写明原因"


# ------------------------------------------------------------------ 8.1 响应次序

def test_the_plans_are_ordered_by_priority_descending(api: Api) -> None:
    """响应 `plans` 按 `priority` 降序（8.1 的另一条验证条件）。

    造法让**请求序与响应序必然相反**：三条单按 C / B / A 建（请求也按这个序提交），
    而本阶段缺出库量 ⇒ 排序降级为「仅按 ABC」（`ABC_ONLY_WEIGHTS`），优先级就是
    A > B > C。若端点原样返回请求序，这条用例当场红 —— 只断言「降序」而不制造一个
    反序的输入，是最容易写成永远绿的那种断言（请求序恰好已经降序，两种实现都过）。
    """
    scenario = api.seed(
        aisles=[AisleSpec("01", cap_total=100, is_near_station=True, station_weight=1.0)],
        materials=[MaterialSpec("MOK", abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no=f"PO-20260908-{n:03d}",
                material_code="MOK",
                qty=QTY_PER_ORDER,
                abc_class=abc,
                batch_no="B26090801",
            )
            for n, abc in enumerate(("C", "B", "A"), start=1)
        ],
    )
    ids = order_ids(scenario)

    body = api.allocate(list(ids)).json()

    priorities = [plan["priority"] for plan in body["plans"]]
    assert priorities == sorted(priorities, reverse=True)
    assert len(set(priorities)) == 3, "三条单的优先级应当互不相等，否则这条用例测不出次序"
    assert [plan["job_order_id"] for plan in body["plans"]] == list(reversed(ids)), (
        "响应次序与请求序相同 —— 说明它没有按 priority 重排"
    )


def test_the_plan_item_carries_the_order_facts(api: Api) -> None:
    """`plans[]` 的字段取自作业单本身（`17` §10.7 的字段表），不是从理由体里回读的。

    `priority` 与理由里的 `priority.score` **同值**（§10.7 的字段表原文）：
    本阶段缺出库量 ⇒ 排序降级为仅按 ABC ⇒ A 类的 `score` 是 1.00。
    """
    scenario = _capacity_scenario(api, orders=1)
    ids = order_ids(scenario)

    (plan,) = api.allocate(ids).json()["plans"]

    assert plan["order_no"] == "PO-20260908-001"
    assert plan["material_code"] == "MOK"
    assert plan["material_name"] == "茉莉柚茶"
    assert plan["abc_class"] == "A"
    assert plan["qty"] == QTY_PER_ORDER
    assert plan["priority"] == 1.0, "A 类的 abc 分量（等级分 ÷ 3）= 1.00"
    assert isinstance(plan["plan_id"], int)


# ------------------------------------------------------------------ 落库形态（D9 第 3 条 / D4）

def test_the_plan_row_is_the_written_payload_and_its_columns_are_a_projection(api: Api) -> None:
    """端点是 `RecommendationPlan` 的**唯一写入方**，且列是 JSON 同名字段的投影。

    D9 第 3 条要求「列与 JSON 的同名字段是同一次写入的投影」。这条在代码上成立，
    在库里可验：`plan_id` 指回 `RecommendationPlan` 的行，两处取自同一份 payload。
    理由体本身**不进响应**（D10：同一份理由有两个副本，就有两个可能分叉的副本），
    故这里只能按 `plan_id` 回库取。
    """
    scenario = _capacity_scenario(api, orders=2)
    ids = order_ids(scenario)

    body = api.allocate(ids).json()

    rows = api.plan_rows()
    assert len(rows) == 2
    assert all(row.plan_kind is PlanKind.ASSIGN for row in rows)
    assert [row.job_order_id for row in rows] == [int(i) for i in ids], "行序即队列序"

    plans = {plan["job_order_id"]: plan for plan in body["plans"]}
    for row in rows:
        payload = ReasonPayload.model_validate(row.payload_json)
        plan = plans[payload.job_id]

        # 列 ↔ JSON：同一次写入的投影
        assert payload.job_id == str(row.job_order_id), "理由体的 job_id 与作业单标识同源同值"
        assert row.degraded == payload.degraded
        assert row.degrade_reason == payload.degrade_reason
        # 响应 ↔ 落库：响应里那条方案指的就是这一行
        assert row.id == plan["plan_id"]
        assert payload.aisles == plan["aisles"]
        assert payload.priority.score == plan["priority"]
        assert payload.predicted_cross_aisle.model_dump() == plan["predicted_cross_aisle"]
        # 理由体自带复算所需的一切：`17` §10.1 的不变式「scores 可由取值复算」要求
        # 复算的人只用这份 JSON —— 他手里没有当时的权重行，故 factors 必须整份落库，
        # 且**是打分时用的那一份**（`scoring.load_weights` 的产物，不是 `settings` 的默认值）。
        assert sorted(payload.factors) == sorted(FACTOR_NAMES)
        assert payload.factors == dict(DEFAULT_WEIGHTS)

    # 存储形态（D4）：非转义 + 键序固定 —— 同一份内容只有一种字节表示。
    raw = api.raw_payload_text(rows[0].id)
    assert raw.startswith('{"aisles"'), f"键序不是固定的升序：{raw[:40]!r}"
    assert "\\u" not in raw, "中文被转义了 —— 引擎没走 app/core/db.py 的 json_serializer"


# ------------------------------------------------------------------ 跨巷道阈值的取数

def test_the_cross_aisle_threshold_comes_from_the_capacity_config_row(api: Api) -> None:
    """阈值取自**生效的** `CapacityConfig` 行，而不是 `settings` 的引导值。

    造法让两种取法的结果**不同**：`02` 里已有该物料的库存（既有 1 条巷道），本次分配
    落到 `01` ⇒ 并集 2 条巷道；配置行给阈值 1 ⇒ `exceeded=true`。若实现读的是
    `settings.same_material_cross_aisle_max`（5），结果是 `false` —— 一条断言就分得开。
    """
    scenario = api.seed(
        aisles=[
            AisleSpec("01", cap_total=100, is_near_station=True, station_weight=1.0),
            AisleSpec("02", cap_total=100, is_near_station=False, station_weight=0.5),
        ],
        materials=[MaterialSpec("MOK", abc_class="A", material_name="茉莉柚茶")],
        inventory=[InventorySpec("020101", "MOK", "B260801", 5)],
        job_orders=[
            JobOrderSpec(
                order_no="PO-20260908-001",
                material_code="MOK",
                qty=QTY_PER_ORDER,
                abc_class="A",
                batch_no="B26090801",
            )
        ],
        capacity={"same_material_cross_aisle_threshold": 1},
    )
    ids = order_ids(scenario)

    (plan,) = api.allocate(ids).json()["plans"]

    assert plan["aisles"] == ["01"]
    assert plan["predicted_cross_aisle"] == {
        "material": 2,
        "threshold": 1,
        "exceeded": True,
    }


def test_an_absent_capacity_config_falls_back_to_the_documented_defaults(api: Api) -> None:
    """容量配置**缺席**（一版都没有）⇒ 用文档默认值，**不阻断**（D3 的「容量」行）。

    与权重缺席（9.1：整批阻断）是两条不同的处置，差别在「有没有权威值可用」：
    权重的默认值是没人批准过的口径，容量的六项在 `16` §353~356 有逐项默认。
    这里只钉住阈值这一项 —— 它是本端点唯一读出来的容量项（其余四项在引擎内按
    `CapacityConfig` 的列默认值生效，端点只负责把行取出来或退回引导值）。
    """
    scenario = _capacity_scenario(api, orders=1, capacity=None)
    ids = order_ids(scenario)

    (plan,) = api.allocate(ids).json()["plans"]

    assert plan["predicted_cross_aisle"]["threshold"] == 5, (
        "`16` §353~356 的同物料跨巷道默认阈值（= settings.same_material_cross_aisle_max）"
    )
    assert plan["predicted_cross_aisle"]["exceeded"] is False


# ------------------------------------------------------------------ 请求的拒绝路径

def test_the_declared_snapshot_version_must_match_the_one_in_use(api: Api) -> None:
    """`snapshot_version` 是**声明**不是筛选器（D16）：与实际不符 ⇒ 409，且一行不写。

    「一行不写」是这条用例真正的分量：拒绝发生在**任何写入之前**。若实现先落方案再比对
    声明，库里的状态与响应就分家了 —— 而调用方看到 409 会以为「什么都没发生」。
    """
    scenario = _capacity_scenario(api, orders=1)
    ids = order_ids(scenario)

    stale = api.allocate(ids, snapshot_version="2026-09-07T00:00")

    assert stale.status_code == 409
    assert stale.json()["error"] == "state_conflict"
    assert "2026-09-07T00:00" in stale.json()["message"]
    assert api.plan_rows() == ()

    matched = api.allocate(ids, snapshot_version=SNAPSHOT_VERSION)

    assert matched.status_code == 200
    assert matched.json()["snapshot_version"] == SNAPSHOT_VERSION


def test_the_response_echoes_the_snapshot_version_of_the_snapshot_in_use(api: Api) -> None:
    """不声明时也回显本次实际用的快照版本 —— 它是 `snapshot_time` 的渲染，不是 `version_no`。"""
    scenario = _capacity_scenario(api, orders=1)

    body = api.allocate(order_ids(scenario)).json()

    assert body["snapshot_version"] == SNAPSHOT_VERSION
    assert _BULK_BATCH_NO_PATTERN.match(body["bulk_batch_no"]), body["bulk_batch_no"]


def test_an_unknown_order_id_rejects_the_whole_batch(api: Api) -> None:
    """未知单号 ⇒ 422 整批拒绝，零方案行（批的原子性）。

    **不放行、不跳过、不猜**：跳过的表现是「响应里少一张单」，而调用方拿到的是 200
    —— 它会以为这一单只是没排上。「未知单号」在库里就是不存在，静默跳过等于把一次
    传参错误记成一次正常的分配结果。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)

    response = api.allocate([ids[0], "999999"])

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert "999999" in response.json()["message"]
    assert api.plan_rows() == (), "被拒的请求不得留下任何方案行"
    assert {order.status for order in api.orders()} == {JobStatus.PENDING}


def test_a_duplicated_order_id_rejects_the_whole_batch(api: Api) -> None:
    """重复单号 ⇒ 422。

    引擎侧对重号是 `ValueError`（`build_priorities`）—— 而端点里一个裸 `ValueError`
    出来就是 500。这里要求它在**进引擎之前**被拦成 422：重号是调用方的报文错误，
    不是服务端的故障。
    """
    scenario = _capacity_scenario(api, orders=1)
    (order_id,) = order_ids(scenario)

    response = api.allocate([order_id, order_id])

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert api.plan_rows() == ()


def test_a_non_canonical_order_id_is_rejected(api: Api) -> None:
    """`"007"` 与 `"7"` 指的是**同一行**，只能有一种写法能过。

    放行两种写法不会报错，只会让同一张单在队列里变成两条队项（`order_queue` 按单据号
    索引），于是它被扣两次容量、在响应里出现两次 —— 而那时已经没有任何环节看得出
    「这两条其实是同一条」。故线上形态钉死为 `str(JobOrder.id)`，前导零一律拒。
    """
    scenario = _capacity_scenario(api, orders=1)
    (order_id,) = order_ids(scenario)

    response = api.allocate([f"0{order_id}"])

    assert response.status_code == 422
    assert response.json()["error"] == "validation_blocked"
    assert api.plan_rows() == ()


def test_a_missing_snapshot_blocks_the_batch(api: Api) -> None:
    """本仓一版快照都没有 ⇒ 409 阻断。

    出库侧的红线（`CLAUDE.md` §四「快照缺失或过期（出库）→ 阻断并提示重新导入，不猜测
    落位」）在阶段四；**这一条是另外的理由**：`snapshot_version` 是响应契约里的必填项，
    没有 `Snapshot` 行就无值可渲染，而 `17` §10.7 也不允许端点自己编一个时点。

    9.5 要的「入库无快照**不阻断**」是**引擎级**的行为（`allocate_batch` 吃
    `SnapshotIndex.absent` 时三个库存类因子降级），与这个端点级关口并存 —— 两处的
    登记见 `design.md`。
    """
    scenario = api.seed(
        aisles=[AisleSpec("01", cap_total=None, is_near_station=True, station_weight=1.0)],
        materials=[MaterialSpec("MOK", abc_class="A", material_name="茉莉柚茶")],
        job_orders=[
            JobOrderSpec(
                order_no="PO-20260908-001",
                material_code="MOK",
                qty=QTY_PER_ORDER,
                abc_class="A",
            )
        ],
        snapshot_time=None,
    )
    ids = order_ids(scenario)

    response = api.allocate(ids)

    assert response.status_code == 409
    assert response.json()["error"] == "blocked_missing_prerequisite"
    assert api.plan_rows() == ()


# ------------------------------------------------------------------ 8.2 状态迁移与批次号回写

def test_the_allocated_orders_are_marked_planned_with_the_batch_number(api: Api) -> None:
    """8.2 的核心：成功者 `bulk_batch_no` 非空**且与响应一致**、状态 `PENDING → PLANNED`。

    「与响应一致」这半句才是重点：端点在响应里算一个批次号、回写时再算一个，两处都
    合法、都不报错，只是作业单上写着 A 批而响应里说 B 批 —— 形状一样，对账时才看得出
    对不上。故这里逐单比对那个串，而不是断言「非空」。

    `lock_version` 一并钉住（D1）：写回改了行，版本就得跟着走。行变了而版本没变，
    下一个带着版本来的调用方（二次确认卡）会以为自己读到的还是旧内容 —— 那正是
    乐观锁最现实的那种失效，且它不会在任何地方报错。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)
    submitted = [ids[0], ids[3]]
    untouched = {ids[1], ids[2], ids[4]}

    before = {order.id: order.lock_version for order in api.orders()}

    body = api.allocate(submitted).json()
    batch_no = body["bulk_batch_no"]
    assert _BULK_BATCH_NO_PATTERN.match(batch_no), f"批次号形状不合 D11：{batch_no}"

    by_id = {order.id: order for order in api.orders()}
    for order_id in submitted:
        order = by_id[int(order_id)]
        assert order.status is JobStatus.PLANNED, f"{order_id} 出了方案却仍停在 {order.status}"
        assert order.bulk_batch_no == batch_no, "作业单上的批次号与响应里的不是同一个"
        assert order.lock_version == before[order.id] + 1

    # 未提交的三条：状态、批次号、版本三件都不动（8.1 的「状态与方案均不变」之上，
    # 再加 8.2 的「只回写被分配的那些」）
    for order_id in untouched:
        order = by_id[int(order_id)]
        assert order.status is JobStatus.PENDING
        assert order.bulk_batch_no is None
        assert order.lock_version == before[order.id]


@pytest.mark.parametrize("status", [JobStatus.PLANNED, JobStatus.REJECTED, JobStatus.CANCELLED])
def test_a_non_pending_order_rejects_the_whole_batch(api: Api, status: JobStatus) -> None:
    """队列里只要有一张单不是 `PENDING` ⇒ **整批拒绝**，不做部分成功（D10 / D12）。

    「部分成功」的形态是：那张已排上计划的单被重新分配、同批的 `PENDING` 单出了方案 ——
    两者分开看都正常，合起来意味着「提交 N 张单得到 M 张方案，而响应里没有任何提示」。
    故这条用例同时钉三件事：报 409、**零方案行**、那张 PENDING 单的状态与批次号也没被动过
    （拒绝发生在写入之前，不是「先写后回滚」）。

    三个状态各有各的现实来路：`PLANNED` = 同一批被提交了两次（见下一条用例）、
    `REJECTED` = 驳回后**忘了重新入队**就再次分配（它要回 `PENDING` 才能再进队列，
    故这里拒得对）、`CANCELLED` = 已移出本次批量（终态，更没有回边）。
    """
    scenario = _capacity_scenario(
        api,
        specs=[_order_spec("PO-20260908-001"), _order_spec("PO-20260908-002", status=status)],
    )
    ids = order_ids(scenario)

    response = api.allocate(ids)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "state_conflict"
    assert body["detail"]["not_pending"] == [
        {"job_order_id": ids[1], "status": status.value}
    ]
    assert body["detail"]["expected"] == JobStatus.PENDING.value

    assert api.plan_rows() == ()
    by_id = {order.id: order for order in api.orders()}
    assert by_id[int(ids[0])].status is JobStatus.PENDING
    assert by_id[int(ids[0])].bulk_batch_no is None


def test_the_failed_order_is_not_written_back(api: Api) -> None:
    """四级走尽的那张单**停留 `PENDING`**、也不领批次号（D12），同批其余单照常回写。

    「失败单算不算这一批」是个真问题：算（照样回写批次号）会让它在作业单上看起来
    已经排过计划，而它一张方案都没有 —— 追溯时「这单属于 BAT-xxx 批」就成了假话，
    且它同时不再是「可重试的待分配单」（`PENDING → PENDING` 是 15 §3.1 那条合法自环）。
    """
    scenario = _capacity_scenario(
        api,
        # 需量 200 板**两条巷都塞不下**（10 / 100），故这张单四级走尽；它在队列里排在前
        # （同优先级按单据号升序），失败后不占容量，后面的单仍应拿到近站台。
        specs=[_order_spec("PO-20260908-001", qty=200), _order_spec("PO-20260908-002")],
    )
    ids = order_ids(scenario)

    body = api.allocate(ids).json()

    assert [plan["job_order_id"] for plan in body["plans"]] == [ids[1]]
    assert aisles_of(body, ids[1]) == ["01"], "失败单占了容量 —— 后面的单没拿到近站台"

    by_id = {order.id: order for order in api.orders()}
    assert by_id[int(ids[0])].status is JobStatus.PENDING
    assert by_id[int(ids[0])].bulk_batch_no is None
    assert by_id[int(ids[1])].status is JobStatus.PLANNED
    assert by_id[int(ids[1])].bulk_batch_no == body["bulk_batch_no"]


def test_submitting_the_same_batch_twice_is_rejected(api: Api) -> None:
    """重复提交同一批：第二次必被拒 —— 8.2 要的「乐观锁语义经状态守卫兑现」。

    这里**没有也不需要**请求侧的版本号：第一次提交把单子推成 `PLANNED`，第二次在同一处
    检查上撞墙。这就是「同一批不会被分配两遍」的全部保证。反过来，若把守卫写在写入循环里
    （逐单判、逐单写），第二次会「前几张写、后几张拒」，而任务书要的是整批拒绝 ——
    故这条用例同时钉住「第二次既不新增方案行、也不动已回写的批次号」。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)
    submitted = [ids[0], ids[3]]

    first = api.allocate(submitted)
    assert first.status_code == 200
    batch_no = first.json()["bulk_batch_no"]
    assert len(api.plan_rows()) == 2

    second = api.allocate(submitted)

    assert second.status_code == 409
    assert second.json()["error"] == "state_conflict"
    assert {item["status"] for item in second.json()["detail"]["not_pending"]} == {
        JobStatus.PLANNED.value
    }
    assert len(api.plan_rows()) == 2, "第二次提交又写了方案行"

    by_id = {order.id: order for order in api.orders()}
    for order_id in submitted:
        assert by_id[int(order_id)].status is JobStatus.PLANNED
        assert by_id[int(order_id)].bulk_batch_no == batch_no


# ------------------------------------------------------------------ 8.3 不写台账、不写 cap

def _cap_rows(api: Api) -> dict[str, tuple]:
    """巷道 cap 的**全部取值列** —— 含 `updated_at`，它是「这一行有没有被改过」的证据。

    `AisleCap.updated_at` 带 `onupdate=utcnow`：行被 UPDATE 过（哪怕改回原值）时间戳就会动。
    故把它一起比，断言的是「这一行**没被碰过**」，比「行数没变」强一档 —— 后者对
    「原地改掉 cap_usable」是瞎的，而那正是本任务要拦的形态。
    """
    with api.factory() as session:
        return {
            cap.aisle_no: (
                cap.id,
                cap.cap_total,
                cap.cap_reserved,
                cap.cap_usable,
                cap.is_near_station,
                cap.updated_at,
            )
            for cap in session.scalars(select(AisleCap).order_by(AisleCap.aisle_no))
        }


def _ledger_count(api: Api) -> int:
    with api.factory() as session:
        return len(session.scalars(select(Ledger)).all())


def test_a_successful_batch_writes_neither_a_ledger_row_nor_a_cap_row(api: Api) -> None:
    """8.3：一次成功的批量分配**不写台账、不写 cap**，作业单停在 `PLANNED`。

    红线「未确认不产生台账」（`CLAUDE.md` §四）在端点层的形态：台账由 `CONFIRMED → EXECUTED`
    那一步写（`15` §6.3），而本端点只到 `PLANNED`。`AisleCap` 一侧同理，但理由不同 ——
    D4 的扣减**只在内存里**：cap 表承载的是这一版快照的**冻结值**，消费它的是阶段四的
    cap 增量事务（`16` §6.3「台账是 cap 增量的唯一来源」）。故这里比的不是行数，而是
    **每一行的每一个取值**（含 `updated_at`）都没动过 —— 「原地把 `cap_usable` 减掉」
    这种改法不会新增行，却会让冻结值当场失真。

    两件事都要验：**没写**（台账 0 行、cap 逐字不变）与**写了**（两条方案行在、状态
    `PLANNED`）。只验前者的话，一个什么都不做、直接 200 的端点同样能过。

    `CapAlert` 不在断言内：它的另一个种类（台账事务增量失败）属阶段四，本端点没有任何
    路径能写出它 —— 写出来才需要讨论。
    """
    scenario = _capacity_scenario(api)
    ids = order_ids(scenario)
    submitted = [ids[0], ids[3]]

    caps_before = _cap_rows(api)
    assert set(caps_before) == {"01", "02"}, "造数变了：本用例的前提是两条巷道的 cap 行"
    assert _ledger_count(api) == 0

    response = api.allocate(submitted)

    assert response.status_code == 200
    # 先证明「确实分配了」—— 否则下面的「什么都没写」对一个空转的端点同样成立
    assert len(api.plan_rows()) == 2

    assert _ledger_count(api) == 0, "批量分配写出了台账 —— 红线是「未确认不产生台账」"
    assert _cap_rows(api) == caps_before, "cap 行的取值被改了 —— D4：扣减只落内存"

    by_id = {order.id: order for order in api.orders()}
    for order_id in submitted:
        assert by_id[int(order_id)].status is JobStatus.PLANNED, "越过了 PLANNED —— 谁确认的？"


# ------------------------------------------------------------------ 8.4 认证与权限口径

def _token_for(api: Api, role: Role) -> str:
    """另建一个该角色的账号并签一份凭据。

    中间件**取凭据里的 `role`、回查库里的 `status`**（见 `app/api/middleware.py` 的模块
    docstring），故这里两处都要给对：账号落库、角色进凭据。
    """
    with api.factory() as session:
        account = Account(
            warehouse_id=settings.warehouse_code,
            username=f"gtj_{role.value}",
            password_hash="端点用例不验口令（见 api 夹具的说明）",
            role=role,
            status=AccountStatus.ACTIVE,
        )
        session.add(account)
        session.commit()
        account_id = account.id
    return create_session_token(account_id, role.value, AccountStatus.ACTIVE.value)


def test_the_endpoint_requires_credentials(api: Api) -> None:
    """未携带凭据 ⇒ 401，且**端点整个没跑**（红线之外的第一道门）。

    四种形态都过一遍：缺头、缺凭据、凭据不是一个 JWT、签名被改过。之所以不是一条就够 ——
    这四条走的是中间件里四条**不同的**分支（`_extract_token` 的两个早退、`decode_session_token`
    的解析失败、签名校验失败），而它们都收敛到同一个 401；少测哪条，就可能有一条被
    「顺手改成放行」而没人发现。

    「端点整个没跑」不是修辞：401 由中间件在路由之前返回，故除了状态码，还要看**库**里
    有没有被动过 —— 一个把鉴权写在端点内部（先取数、再检查）的实现同样会回 401，
    但它已经把方案行写进去了。
    """
    scenario = _capacity_scenario(api, orders=1)
    (order_id,) = order_ids(scenario)
    body = {"warehouse_id": WAREHOUSE_ID, "job_order_ids": [order_id]}

    cases: tuple[tuple[str, dict[str, str]], ...] = (
        ("没有 Authorization 头", {}),
        ("只有方案名、没有凭据", {"Authorization": "Bearer"}),
        ("凭据不是一个 JWT", {"Authorization": "Bearer not-a-jwt"}),
        ("凭据的签名被改过", {"Authorization": f"Bearer {api.token}x"}),
    )
    for label, headers in cases:
        response = api.client.post(ALLOCATE_URL, json=body, headers=headers)
        assert response.status_code == 401, f"{label} —— 竟然没被挡下"
        assert response.json()["error"] == "unauthenticated"
        assert api.plan_rows() == (), f"{label} —— 端点跑到写入了"

    # 作业单也没被动过（8.2 的两处回写都没发生）
    assert {order.status for order in api.orders()} == {JobStatus.PENDING}
    assert {order.bulk_batch_no for order in api.orders()} == {None}


@pytest.mark.parametrize(
    "role", [Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR, Role.ADMIN]
)
def test_the_holders_of_inbound_operate(role: Role) -> None:
    """D10 的「资源标识 = `inbound.operate`」在本端点上的读数：四个角色各自的答案。

    矩阵的权威在 `tests/logic/test_permissions.py`（`13` §2.2 逐行抄录的整张表 + 「与
    `ROLE_PERMISSIONS` 互为反演」的断言），**本条是它在本端点侧的复述、不是第二份权威** ——
    这一份留给读端点契约的人：D10 把本端点的资源标识定成了 `inbound.operate`，而「谁能触发
    这个动作」是接着要问的第一个问题，去别处找表多半会按直觉补一格（最容易补错的正是主管 ——
    他连 `inbound.view` 都没有，理由是「入库执行不归主管」）。
    """
    expected = role in (Role.WAREHOUSE_KEEPER, Role.ADMIN)
    assert check(role, Permission.INBOUND_OPERATE) is expected
    # 引擎调用不是可授予的资源（`AUTO_ONLY`，`check` 里这条先于查表）—— 本端点会触发引擎，
    # 但触发它的是 `inbound.operate` 这个**业务动作**，不是 `engine.invoke` 这个资源。
    assert check(role, Permission.ENGINE_INVOKE) is False


@pytest.mark.parametrize(
    "role", [Role.WAREHOUSE_KEEPER, Role.PLANNER, Role.SUPERVISOR, Role.ADMIN]
)
def test_the_endpoint_enforces_inbound_operate(api: Api, role: Role) -> None:
    """端点级资源鉴权（`inbound.operate`，D10 的资源标识）：仅仓管员/管理员 200，计划员/主管 403。

    授权依据是**业务动作**（`inbound.operate`），不是 `engine.invoke`：引擎调用是业务动作的
    内部后果（`engine.invoke` 恒 `AUTO_ONLY`，`check` 对任何角色都返回 False）。故本端点挂
    `require_permission(inbound.operate)` —— 谁有 `inbound.operate` 谁就能触发它，主管连
    `inbound.view` 都没有（`13` §2.2「入库执行不归主管」）。

    403 时还要断言**端点整个没跑**（方案行、状态、批次号都没动）：一个把鉴权写在端点内部
    （先取数、再检查）的实现同样回 403，但库里已经写进去了。
    """
    scenario = _capacity_scenario(api, orders=1)
    (order_id,) = order_ids(scenario)
    expected = 200 if check(role, Permission.INBOUND_OPERATE) else 403

    response = api.client.post(
        ALLOCATE_URL,
        json={"warehouse_id": WAREHOUSE_ID, "job_order_ids": [order_id]},
        headers={"Authorization": f"Bearer {_token_for(api, role)}"},
    )

    assert response.status_code == expected, role.value
    if expected == 200:
        assert len(api.plan_rows()) == 1
        assert api.orders()[0].status is JobStatus.PLANNED
    else:
        assert response.json()["error"] == "permission_denied"
        assert api.plan_rows() == ()
        assert api.orders()[0].status is JobStatus.PENDING
        assert api.orders()[0].bulk_batch_no is None


# ------------------------------------------------------------------ 8.5 规模上限与空批

def test_more_than_the_limit_is_rejected_and_told_to_split(api: Api) -> None:
    """51 单 ⇒ 422，**零方案**，且提示语要告诉调用方「拆开提交」（spec「超上限被拒绝」）。

    这一条守的是**不静默截断**：把前 50 条分配掉、第 51 条丢掉，是最省事也最坏的一种
    「处理」—— 调用方拿到一份看起来完整的 `plans`，永远不会知道有一条单没进队列。故断言
    分两半：**方案行数为 0**（不是「≤50」）＋ 那 5 条真单**三件全不动**（状态、批次号）。

    **提交形状 = 5 条真单 + 46 条不存在的号**，这个混搭是有意的，它同时钉到第二件事：
    上限判在**取数之前**。若把长度检查放到 `_load_orders` 之后，这 46 个号会先撞「未知单号」
    而给出另一个错误 —— 于是断言「拿到的是上限的错误」= 断言「上限的判决先于查库」。
    端点里两类 4xx 的先后因此不靠读代码确认（与 8.1 的「报文能当场判错的先全判掉」同一条）。
    """
    scenario = _capacity_scenario(api, orders=5)
    submitted = [
        *order_ids(scenario),
        *(str(900_000 + n) for n in range(46)),   # 5 + 46 = 51
    ]
    assert len(submitted) == MAX_JOB_ORDERS_PER_BATCH + 1, "造数形状变了，上限用例就不再是上限用例"

    response = api.allocate(submitted)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_blocked"
    assert body["detail"] == {
        "submitted": len(submitted),
        "max": MAX_JOB_ORDERS_PER_BATCH,
    }
    # 「提示拆分」是规格里的措辞，不是修饰：只说「超限了」的提示语，调用方还得自己去
    # 猜上限是多少、下一步该做什么。
    message = body["message"]
    assert str(MAX_JOB_ORDERS_PER_BATCH) in message
    assert "拆" in message

    # 没有部分成功：一条方案行都没有，真单一条都没被回写
    assert api.plan_rows() == ()
    for order in api.orders():
        assert order.status is JobStatus.PENDING
        assert order.bulk_batch_no is None


def test_the_limit_counts_what_was_submitted_not_the_deduplicated_set(api: Api) -> None:
    """上限判**原始条数**：51 条里带一条重号（实为 50 张单）同样拒绝。

    这条钉的是一个**取舍**，不是一处显然的对错：`_requested_ids` 会去重，于是「判原始条数」
    与「判去重后的条数」都能自圆其说。取前者，理由写在 `_require_batch_size` 的 docstring 里
    —— 后者会让一份 60 条带 10 条重号的请求以「实为 50 条」被放行，那依然是**静默削减**，
    只是削减发生在去重那一步。去重是防「同一张单被扣两次」的**正确性**检查，不该顺带
    承担「把超限的请求压回上限以内」这件事。

    顺带钉住判决次序：若长度检查被挪到 `_requested_ids` 之后，这里拿到的是重号的错误，
    而重号检查本就在取数之前、也是个 422 —— **两者错误码相同**，只有 `detail` 分得开。
    故断言 `detail` 而不是只断言 `error`：状态码相同的两条路径，靠状态码是分不出来的。
    """
    scenario = _capacity_scenario(api, orders=5)
    real = order_ids(scenario)
    submitted = [
        *real,
        real[0],                                   # 重号：51 条里有 1 条是重复的
        *(str(900_000 + n) for n in range(45)),    # 5 + 1 + 45 = 51
    ]
    assert len(submitted) == MAX_JOB_ORDERS_PER_BATCH + 1

    response = api.allocate(submitted)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_blocked"
    assert body["detail"] == {
        "submitted": len(submitted),
        "max": MAX_JOB_ORDERS_PER_BATCH,
    }, "拿到重号的 detail 说明长度检查被挪到了去重之后 —— 判决次序与判据宽度都变了"
    assert api.plan_rows() == ()


def test_an_empty_batch_produces_empty_plans(api: Api) -> None:
    """空数组 = 明确的空集（`17` §10.7 的硬要求），不是「全量」。

    产出空 `plans` / 空 `degraded_alerts`、零方案行、零回写；**但仍是一次成功的往返** ——
    `bulk_batch_no` 与 `snapshot_version` 照常回显，因为契约把这两个字段定成必填，而
    「这一批用了哪一版快照」对一次空批同样是可回答的（就是当前快照）。

    顺带钉住 D11 的一个推论：日序数 `NN` 是**计数**（当日已有批次号的数量 + 1）不是**计数器**，
    故空批不消耗号 —— 连调两次拿到同一个串。这条是 D2「同样输入必得同样输出」在批次号上的
    直读，而不是「两次调用撞了号」的 bug（空批没有成员，没有任何 `JobOrder` 会领走它）。
    """
    _capacity_scenario(api, orders=5)

    first = api.allocate([])
    assert first.status_code == 200
    body = first.json()
    assert body["plans"] == []
    assert body["degraded_alerts"] == []
    assert body["snapshot_version"] == SNAPSHOT_VERSION
    assert _BULK_BATCH_NO_PATTERN.match(body["bulk_batch_no"])

    assert api.plan_rows() == ()
    for order in api.orders():
        assert order.status is JobStatus.PENDING
        assert order.bulk_batch_no is None

    second = api.allocate([])
    assert second.status_code == 200
    assert second.json()["bulk_batch_no"] == body["bulk_batch_no"], (
        "空批不该消耗批次号 —— D11 的 NN 是当日已有号的计数，无号被领走则序号不前移"
    )


# ------------------------------------------------------------------ 9.1 权重缺失 ⇒ 整批阻断

def test_a_missing_weight_version_blocks_the_whole_batch(api: Api) -> None:
    """无生效权重版本 ⇒ 409 阻断、零方案（spec「无生效版本时阻断」/ D3 的「权重」行）。

    **这一条是阻断，不是降级**，两件容易混的事在这里正好分开：同一批里**缺某个因子**
    的取值时（无快照 / 无库存 / 批号为空 / 无站台行）引擎照常出方案、只在理由里标降级
    （D8、9.3、9.5 各验一处）；而缺**权重**时引擎连「该按什么比例算总分」都不知道，
    没有可降级的目标 —— 故阻断。造数侧用 `weights=None`（`make_scenario` 的显式口径：
    不建任何权重行），这正是 D3 说的「配置尚未就绪」。

    判据取「一版都没有」这一支（`configured == 0`）：提示语据行数分成两种，操作员的动作
    也不同 —— 一版都没配是「去配一版」，配了但都没到生效时间是「等它生效或改生效时间」。
    另一支（已配未生效）由逻辑层的 `load_weights` 用例覆盖，这里不重造第二份。

    阻断发生在**取数**这一步（`load_weights` 抛），在引擎之前、在任何写入之前 ⇒ 零方案、
    零回写。这也顺带说明为什么它不该是「按 0 权重的退化方案」：那会产出一份看起来正常
    的方案，而它背后没有任何一个人类批准过的比例。
    """
    scenario = _capacity_scenario(api, orders=5, weights=None)
    submitted = order_ids(scenario)[:2]

    response = api.allocate(submitted)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "blocked_missing_prerequisite"
    # spec 原话是「返回『权重配置未就绪』类提示」—— 断言这句而不是断言提示语逐字相等，
    # 提示语里还带着仓库号与排查方向（两种成因分开说），那些是给人读的，不是契约。
    assert "权重配置未就绪" in body["message"]

    assert api.plan_rows() == ()
    for order in api.orders():
        assert order.status is JobStatus.PENDING
        assert order.bulk_batch_no is None


# ------------------------------------------------------------------ 9.4 写库异常 ⇒ 整批回滚

#: 注入式失败的标记串。`match=` 用它而不是 `.+`：断言红的就是这次注入，
#: 顺带让「真的库错被误当成注入错」在失败信息里一眼看得见。
INJECTED_WRITE_FAILURE = "注入点：第 3 条单落库时写库异常"


def test_a_write_failure_on_the_third_plan_rolls_back_the_whole_batch(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第 3 条单落库时抛错 ⇒ 前两条**已写完**的也一并消失（`design.md` D12「写库异常」行）。

    D12 的原话是「一个事务内完成全部写入，异常即整体回滚 —— 不留『一半单有方案、一半
    没有』的中间态」。这条不变式的两个半边写在不同文件里：**「一个事务」**在端点（写
    循环里没有 `commit()`，循环走完才提交一次），**「异常即回滚」**在依赖
    （`deps.get_db` 的 `except Exception: session.rollback(); raise`）。故用例必须造出
    「已经写进两条、第三条才炸」这个时点 —— 第 1 条就炸的话，回滚与不回滚看起来一样。

    | 断言 | 钉的是哪件事 |
    |---|---|
    | `attempts == 三条单号` | 注入确实落在第 3 条（前两条真的 `add` + `flush` 进过事务） |
    | `plan_rows() == ()` | 前两行 `RecommendationPlan` 没留在库里 |
    | 三单 `PENDING` / `bulk_batch_no is None` | 前两单的回写（批次号 + 状态迁移）同样没留 |

    第一条断言是防**空转**的：若注入点挪到第 1 条，后两条断言会照绿（什么都没写进去），
    那时这条用例证明的东西是零。

    落地时跑过四处变异，红点位置如下：

    | 变异 | 红在哪 |
    |---|---|
    | 把 `commit()` 挪进写循环（「一个事务」破了） | `plan_rows() == ()` |
    | 撤掉 `get_db` 的回滚**与**关闭（「异常即回滚」破了） | `plan_rows() == ()` |
    | 回写后立刻提交（方案与回写分两个事务） | `plan_rows() == ()` |
    | 把注入点挪到第 1 条（测试侧自检） | `attempts` 那条 |

    三处产品侧变异都红在**同一行**，这是实话：单个事务里「前两行在不在」与「前两单的
    状态 / 批次号在不在」是**同一次回滚的两个现场**，制造不出「方案回滚了而回写留下了」
    的形态 —— 故表里后两条是**同因的第二现场**，不是独立判据（回写本身发生没发生由 8.2
    的用例独立钉住）。第四处说明 `attempts` 那条防线不是装饰。

    **它分不开的东西**：撤掉 `rollback()` 与撤掉 `session.close()` 在这条用例看来一样
    （两者都让未提交的改动消失），故它钉的是「库里不留中间态」这个可观察结果，而不是
    `get_db` 里那两行的写法。

    读库走的是 `factory` 开的**新会话**（`api.plan_rows` / `api.orders`），所以只有已提交
    的写入可见 —— 这正是「一半有方案、一半没有」在操作员眼里会呈现的样子。

    ## 为什么落在 `tests/api/` 而不是任务书写的 `tests/logic/`

    见模块 docstring 末节：`app/engine/` 按 D4 从不碰会话（扣减只落内存），写库整段都在
    端点；而 `tests/logic/conftest.py` 的 `session` 夹具把用例包在一层外层事务里、内层用
    savepoint，端点里的 `commit()` 只会释放那个 savepoint —— 在逻辑层写这条用例，外层事务
    最后整体回滚，**换个写法它都绿**，是一份假保证。真库里「有没有一半写进去」只有走真实
    会话边界才看得见，那正是本夹具（独立库 + `app.state.session_factory` 覆盖）做的事。
    **这是本阶段第二处按落点现实调整的位置**（第一处是 8.4）。

    ## 注入点选 `RecommendationPlan` 的构造，不选数据库

    要的是「写到第 3 条时异常」这个**位置**，不是某种特定的写失败形态 —— 主键冲突 / 磁盘满
    / 连接断在回滚语义上是同一件事（都是事务中途抛）。所以不制造真的库错，而是把
    `app.api.routes.allocate` 里的 `RecommendationPlan` 这个名字换成一个计数工厂：前两次
    照常返回真行，第 3 次抛。这依赖一个实现事实：那边用的是**模块全局**里的
    `RecommendationPlan`（不是在函数内重新 import），且该模块有
    `from __future__ import annotations`，换掉这个名字不会连带改变任何注解的求值。
    """
    scenario = _capacity_scenario(api, orders=3)
    submitted = order_ids(scenario)
    attempts: list[int] = []
    real_plan = RecommendationPlan

    def _explode_on_the_third(**kwargs):
        """前两条真建、第 3 条抛 —— 失败点落在「已经写了两行」之后才有回滚可验。"""
        attempts.append(kwargs["job_order_id"])
        if len(attempts) == 3:
            raise RuntimeError(INJECTED_WRITE_FAILURE)
        return real_plan(**kwargs)

    monkeypatch.setattr(
        "app.api.routes.allocate.RecommendationPlan", _explode_on_the_third
    )

    with pytest.raises(RuntimeError, match=INJECTED_WRITE_FAILURE):
        api.allocate(submitted)

    assert attempts == [int(order_id) for order_id in submitted], (
        "注入必须落在第 3 条上：前两条已经 add + flush 进事务，才有中间态可验"
    )
    assert api.plan_rows() == ()
    orders = api.orders()
    assert [order.status for order in orders] == [JobStatus.PENDING] * 3
    assert [order.bulk_batch_no for order in orders] == [None] * 3
