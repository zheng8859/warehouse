"""度量与身份链 2 实体的契约测试（tasks.md §5 的验证）。

事实来源：17-数据模型设计 §五（KPI 实体）、§六（身份实体）、§九（枚举）、
          §10.6（KPI 卡片 JSON）、§十一（数据隔离）、§十二（留存）
          18-KPI 与验收度量设计 §3.1（指标快照状态机）、§3.2（状态详解）、§3.3（实体）
          13-权限分级与访问控制系统 §5.1（状态流转图）、§5.2（账号数据模型）
          openspec/changes/data-model-permission/design.md D2 / D3 / D4 / D8 / D12
          spec `data-model`「枚举登记范围与取值」「account_status 接受 rejected」
          spec `auth`「账号由管理员开通」「账号状态迁移」「初始凭据与首次改密」

**取值来源**：KPI 卡片的每个数字都取自 17 §10.6 的示例 JSON（`2026-W37` / 4 / 5 / 4.8 /
3.2 / 0.68 / 0.995），不是编的；账号的用户名与密码哈希是**合成值** —— 文档只给字段
不给样本（已在用例里注明）。`$2b$12$` 开头的哈希串是 bcrypt 的真实形态（60 字符）。

**CHECK 类用例分两种写法**，理由同 `test_job.py`：取值类走原生 `text()`（Python 层
先抛，ORM 路径从没碰过 DB 的 CHECK），结构类走 ORM（SQLite 忽略 `VARCHAR(n)` 长度，
这类约束本来就要落到 DB 才生效）。

**本文件里两条「反向」断言**（`test_..._is_an_entity_local_domain`、
`test_shared_enum_registry_is_still_eleven`）守的是 D2：11 个共享枚举是可核对的口径，
把局部值域塞进 `enums.py` 会让这个口径失效。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.enums import AccountStatus, Role
from app.models.base import Base
from app.models.identity import Account
from app.models.kpi import KpiSnapshot, KpiStatus

pytestmark = pytest.mark.model

WAREHOUSE = "GTJ10036"
NOW = "2026-09-11 08:00:00"

#: 17 §10.6 的 KPI 卡片示例，逐字照抄。
CARD_JSON = {
    "period": "2026-W37",
    "weighted_concentration": {"value": 4, "threshold_n": 5, "status": "PASS"},
    "same_material_cross_aisle": {"value": 4.8, "threshold": 5, "status": "PASS"},
    "same_batch_cross_aisle": {"value": 3.2, "threshold": 3, "status": "DEVIATION"},
    "adoption_rate": 0.68,
    "placement_accuracy": 0.995,
}


# ------------------------------------------------------------------ 夹具工厂

def _kpi(session: Session, **overrides) -> KpiSnapshot:
    """KPI 快照。默认值 = 17 §10.6 那一周（2026-W37）的全部指标。"""
    fields = {
        "warehouse_id": WAREHOUSE,
        "period": "2026-W37",
        "status": KpiStatus.READY,
        "weighted_concentration": 4.0,
        "same_material_cross_aisle": 4.8,
        "same_batch_cross_aisle": 3.2,
        "adoption_rate": 0.68,
        "placement_accuracy": 0.995,
        "card_json": dict(CARD_JSON),
    }
    fields.update(overrides)
    row = KpiSnapshot(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _account(session: Session, **overrides) -> Account:
    """账号。用户名与哈希是**合成值**（文档未给样本）。"""
    fields = {
        "warehouse_id": WAREHOUSE,
        "username": "gtj_keeper",
        "password_hash": "$2b$12$" + "0123456789abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOP",
        "role": Role.WAREHOUSE_KEEPER,
        "status": AccountStatus.ACTIVE,
    }
    fields.update(overrides)
    row = Account(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _raw_insert(session: Session, table: str, **values) -> None:
    """原生 INSERT —— 取值类 CHECK 用例必须绕过 ORM（D3，见模块 docstring）。"""
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    session.execute(
        text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values
    )


# ------------------------------------------------------------------ KpiSnapshot：一行/周期

def test_kpi_card_json_round_trips_as_dict(session: Session) -> None:
    """tasks 5.1 的验证动作：写入 17 §10.6 的卡片后读回**结构**（dict），不是字符串。

    这条看着平凡，实则钉住两件事：列是 SQLAlchemy `JSON` 而非 `Text`（D4），
    且引擎配了 `json_deserializer` —— SQLite 下没有它，读回来就是一串文本。
    """
    row = _kpi(session)
    session.expire_all()

    card = session.get(KpiSnapshot, row.id).card_json  # type: ignore[union-attr]
    assert isinstance(card, dict)
    assert card["weighted_concentration"]["threshold_n"] == 5
    assert card["same_batch_cross_aisle"]["status"] == "DEVIATION"


def test_one_snapshot_per_period_per_warehouse(session: Session) -> None:
    """一个仓库一个周期只有一行（18 §3.3「每次周期聚合」= 一行）。

    18 §3.1 的 `READY → STALE → PENDING` 与 `ERROR → PENDING` 都是**同一行**的状态
    变化（重算），不是新增行 —— 若同期可多行，看板「查最新」就会随机取到旧的那行。
    """
    _kpi(session)
    with pytest.raises(IntegrityError):
        _kpi(session)
    session.rollback()


def test_recomputing_a_period_updates_the_same_row(session: Session) -> None:
    """重算不新增行：状态回到 `PENDING` 后重新算完，仍是原来那一行（18 §3.1）。"""
    row = _kpi(session)

    row.status = KpiStatus.STALE
    session.flush()
    row.status = KpiStatus.PENDING
    session.flush()
    row.status = KpiStatus.READY
    row.weighted_concentration = 3.0
    session.flush()
    session.expire_all()

    assert session.execute(text("SELECT count(*) FROM kpi_snapshots")).scalar_one() == 1
    assert session.get(KpiSnapshot, row.id).weighted_concentration == 3.0  # type: ignore[union-attr]


def test_kpi_unique_key_starts_with_the_warehouse(session: Session) -> None:
    """唯一键 = `(warehouse_id, period)`，列序即「看板查最新」的索引（design.md 性能目标表）。

    18 §「前端查询 `/api/kpi/snapshot` 获取最新 KpiSnapshot」走的就是这个前缀 ——
    与 `AisleCap` 同一处置：唯一约束兼作查询索引，不另建重复索引。
    """
    constraints = [
        c
        for c in Base.metadata.tables["kpi_snapshots"].constraints
        if isinstance(c, sa.UniqueConstraint)
    ]
    assert len(constraints) == 1
    assert list(constraints[0].columns.keys()) == ["warehouse_id", "period"]


# ------------------------------------------------------------------ KpiSnapshot：状态机（18 §3.1）

def test_kpi_status_covers_the_state_machine_in_18(session: Session) -> None:
    """五个状态 = 18 §3.1/§3.2 的状态机，逐字对应（取值即状态名，与 `AlertKind` 同一处理：
    成员名 ASCII、取值照文档 —— 它们会直接出现在看板的「数据过期」标注里）。"""
    assert {s.value for s in KpiStatus} == {
        "PENDING",
        "COMPUTING",
        "READY",
        "STALE",
        "ERROR",
    }


def test_kpi_status_is_an_entity_local_domain() -> None:
    """D2：KPI 状态是**实体局部值域**，不进 `enums.py`。

    D2 逐条列出的四处局部值域来自 17 的实体字段表；本处来自 18 §3.1 的状态机 ——
    同一条口径（只在本实体内有意义的值域，不进共享登记表）。
    """
    assert KpiStatus.__module__ == "app.models.kpi"
    assert not hasattr(shared_enums, "KpiStatus")


def test_kpi_status_defaults_to_pending(session: Session) -> None:
    """触发即 `PENDING`（18 §3.1 的入口状态）。

    默认值不是顺手给的：若默认为 `READY`，计算失败的行会被看板当成就绪数据展示 ——
    正是 18 §「不得静默显示空或错值」要拦的。
    """
    row = KpiSnapshot(warehouse_id=WAREHOUSE, period="2026-W38")
    session.add(row)
    session.flush()
    assert row.status is KpiStatus.PENDING


def test_kpi_status_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层：`validate_strings=True` 在绑定参数时即抛 `StatementError`。"""
    with pytest.raises(StatementError):
        _kpi(session, status="快照就绪")


def test_kpi_status_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：绕过 ORM 的写入由 DB CHECK 拒绝。状态拼错 → 看板查不到该行。"""
    with pytest.raises(IntegrityError):
        _raw_insert(
            session,
            "kpi_snapshots",
            warehouse_id="'" + WAREHOUSE + "'",
            period="'2026-W37'",
            status="'READY '",
            created_at="'" + NOW + "'",
        )
    session.rollback()


# ------------------------------------------------------------------ KpiSnapshot：指标可缺席

def test_metric_values_may_be_absent_before_the_snapshot_is_ready(session: Session) -> None:
    """`PENDING` / `COMPUTING` / `ERROR` 的行**必须能不带指标值入库**（18 §3.2）。

    状态表逐行写明：`PENDING` 时「可获取的信息 = 周期、仓库号」，`ERROR` 时只有错误
    描述。若把指标列写成 NOT NULL，18 的状态机在存储层直接落不了地 —— 所以这一组
    列可空不是宽松，是状态机的要求。
    """
    row = KpiSnapshot(warehouse_id=WAREHOUSE, period="2026-W39")
    session.add(row)
    session.flush()
    session.expire_all()

    fresh = session.get(KpiSnapshot, row.id)
    assert fresh.status is KpiStatus.PENDING  # type: ignore[union-attr]
    for name in (
        "weighted_concentration",
        "same_material_cross_aisle",
        "same_batch_cross_aisle",
        "adoption_rate",
        "placement_accuracy",
        "card_json",
    ):
        assert getattr(fresh, name) is None, f"{name} 在 PENDING 行上不该有值"


def test_first_period_has_no_chain_ratio(session: Session) -> None:
    """「环比」可空 —— 第一期的上一期不存在（18 §151：环比取自 KpiSnapshot 历史）。

    **落 JSON 列而非数值列**：17 §5.1 列了这个字段但全文没给口径（是哪个指标的环比、
    单值还是分指标各一个），用单值列就把口径钉死了。口径定了再改成数值列 + 迁移。
    """
    row = _kpi(session, period_over_period_json=None)
    assert row.period_over_period_json is None

    other = _kpi(session, period="2026-W38")
    other.period_over_period_json = {"weighted_concentration": -0.25}
    session.flush()
    session.expire_all()

    raw = session.execute(
        text("SELECT period_over_period_json FROM kpi_snapshots WHERE id = :i"),
        {"i": other.id},
    ).scalar_one()
    assert json.loads(raw) == {"weighted_concentration": -0.25}


def test_rates_are_null_rather_than_zero_without_a_sample(session: Session) -> None:
    """无样本时采纳率 / 落位准确率为 **NULL 而不是 0**。

    0 会污染看板：0% 采纳率看起来像「全线崩盘」，而真实语义是「本周还没有可采纳的
    决策」。这与 `Aisle.is_near_station` 用 NULL 表达「未导出」是同一处置 ——
    0 / False 是**一个具体的值**，不能拿来表示「没有值」。
    """
    row = _kpi(session, adoption_rate=None, placement_accuracy=None)
    session.flush()
    session.expire_all()
    fresh = session.get(KpiSnapshot, row.id)
    assert fresh.adoption_rate is None  # type: ignore[union-attr]
    assert fresh.placement_accuracy is None  # type: ignore[union-attr]


# ------------------------------------------------------------------ Account：唯一与状态

def test_username_is_globally_unique_across_warehouses(session: Session) -> None:
    """用户名**全局唯一**（17 §6.1），不是「仓库内唯一」。

    这是 `warehouse_id` 过滤维度的一处刻意例外：账号是运维主体，同一人在两个厂的
    库里仍是同一个人；按仓库分域会让「同用户名两个账号」看起来合法，而登录页只有
    一个用户名输入框 —— 撞名即无法定位账号。故唯一键落在 `username` 单列上。
    """
    _account(session)
    with pytest.raises(IntegrityError):
        _account(session, warehouse_id="GTJ99999")
    session.rollback()


def test_new_account_is_pending_with_password_unchanged(session: Session) -> None:
    """新建账号默认 `pending` + 初始密码未修改（13 §5.1 状态图、spec `auth`）。

    「未修改」= `False`，不是「已修改」—— 它是**强制改密的开关**，反了就等于
    新账号可以直接用初始密码长期登录。
    """
    row = Account(
        warehouse_id=WAREHOUSE,
        username="gtj_planner",
        password_hash="$2b$12$" + "0123456789abcdefghijklmnopqrstuvwxyz" + "ABCDEFGHIJKLMNOP",
        role=Role.PLANNER,
    )
    session.add(row)
    session.flush()

    assert row.status is AccountStatus.PENDING
    assert row.initial_password_changed is False
    assert row.last_login_at is None, "还没登录过就不该有登录时间"


def test_rejected_is_a_legal_status(session: Session) -> None:
    """spec `data-model` 场景「account_status 接受 rejected」：驳回是可落库的合法取值，
    不是自由字符串（17 §九 / 13 §5.2 已订正进枚举）。"""
    row = _account(session, status=AccountStatus.REJECTED)
    session.flush()
    session.expire_all()
    assert session.get(Account, row.id).status is AccountStatus.REJECTED  # type: ignore[union-attr]


def test_account_status_values_are_lowercase_and_four() -> None:
    """D3：`Role` 与 `AccountStatus` 保持 lowercase，**不要顺手统一**成大写。

    取值大小写不只是风格：`enum_column` 把它们写进 CHECK，库里的字面量就是这些
    lowercase 串，会话凭据的 `status` claim 也照此签发（13 §7.2）。
    """
    assert {s.value for s in AccountStatus} == {"pending", "active", "disabled", "rejected"}


def test_illegal_account_status_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层。"""
    with pytest.raises(StatementError):
        _account(session, status="ENABLED")


def test_illegal_account_status_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：绕过 ORM 的写入由 DB CHECK 拒绝。

    账号状态是**认证路径**的输入（中间件直接读凭据里的 `status`，13 §6.1）——
    一个拼错的 `status` 若进库，该账号的判定行为将不可预测。
    """
    with pytest.raises(IntegrityError):
        _raw_insert(
            session,
            "accounts",
            warehouse_id="'" + WAREHOUSE + "'",
            username="'gtj_keeper'",
            password_hash="'x'",
            role="'warehouse_keeper'",
            status="'pending '",
            initial_password_changed="0",
            created_at="'" + NOW + "'",
        )
    session.rollback()


def test_role_is_limited_to_the_four_documented_values(session: Session) -> None:
    """角色限 13 §一 的四类（17 §九 的 `role`）。同样两层，此处验 DB 那层。"""
    with pytest.raises(IntegrityError):
        _raw_insert(
            session,
            "accounts",
            warehouse_id="'" + WAREHOUSE + "'",
            username="'gtj_admin2'",
            password_hash="'x'",
            role="'super_admin'",
            status="'pending'",
            initial_password_changed="0",
            created_at="'" + NOW + "'",
        )
    session.rollback()


def test_account_stores_only_a_hash_never_a_plaintext_password() -> None:
    """表上**只有** `password_hash`，没有存放明文的列（spec `auth`：不得以明文存储）。

    这条守的是模型的形状而非某一次写入 —— 明文列一旦存在，迟早会被某条「临时」路径
    写进去。API 层「响应中不回显密码或哈希」的断言属 §7。
    """
    columns = set(Base.metadata.tables["accounts"].c.keys())
    assert "password_hash" in columns
    for forbidden in ("password", "password_plain", "plain_password", "initial_password"):
        assert forbidden not in columns, f"{forbidden} 不该存在：明文口令不得落库"


# ------------------------------------------------------------------ Account：创建人自引用

def test_creator_is_a_self_reference_and_may_be_absent(session: Session) -> None:
    """`created_by` 指向账号自己，**可空** —— 首个管理员由谁创建？

    13 §5.1 的状态图从「管理员创建账号」开始，而第一个管理员没有任何人有权限创建它
    （自举问题：种子/运维直接落库）。用 NULL 表达「基建写入、无创建人」，不编造一个
    `system` 账号来满足外键 —— 那会让「谁创建了谁」的审计链上多出一个假节点。
    """
    root = _account(session, username="gtj_admin", role=Role.ADMIN)
    child = _account(session, username="gtj_planner", role=Role.PLANNER, created_by_id=root.id)
    session.flush()

    assert root.created_by_id is None
    assert child.created_by_id == root.id


def test_creator_must_be_an_existing_account(session: Session) -> None:
    """创建人不可指向不存在的账号（`foreign_keys=ON`，D12）。"""
    with pytest.raises(IntegrityError):
        _account(session, created_by_id=999_999)
    session.rollback()


def test_creator_is_a_self_referential_foreign_key() -> None:
    """外键目标 = `accounts.id` 自身（17 §6.1 的「创建人」是账号引用，不是自由文本）。"""
    column = Base.metadata.tables["accounts"].c.created_by_id
    targets = {fk.target_fullname for fk in column.foreign_keys}
    assert targets == {"accounts.id"}, f"created_by_id 指向了意外的表：{targets}"


# ------------------------------------------------------------------ 共享枚举登记范围（D2 / 17 §九）

def test_shared_enum_registry_is_still_eleven() -> None:
    """tasks 5.4：`AccountStatus` 补 `REJECTED` 是**改取值不是改数量** ——
    17 §九 的「共 11 个枚举」这个可核对口径必须仍然成立。"""
    registered = [
        name
        for name in dir(shared_enums)
        if isinstance(getattr(shared_enums, name), type)
        and issubclass(getattr(shared_enums, name), shared_enums.Enum)
        and getattr(shared_enums, name).__module__ == "app.core.enums"
    ]
    assert len(registered) == 11, f"共享枚举个数变了：{sorted(registered)}"

    # 逐条点名，避免「数量对了但换了一个」这种假通过。
    assert set(registered) == {
        "JobType", "JobStatus", "ImportStatus", "FileType", "LedgerType",
        "Disposition", "AbcClass", "Role", "AccountStatus", "VerifyResult", "ItemStatus",
    }


def test_account_status_now_carries_rejected() -> None:
    """spec `data-model` 场景与 D2 的另一半：`AccountStatus` 的第四个取值就是 `REJECTED`。"""
    assert hasattr(AccountStatus, "REJECTED")
    assert AccountStatus.REJECTED.value == "rejected"


def test_kpi_and_identity_entities_carry_the_isolation_column() -> None:
    """§十一：全部实体携带 `warehouse_id`（Account 的唯一例外是用户名唯一键的**范围**，
    不是它不需要该列 —— 账号仍按仓库维度做数据范围区分，13 §3.2）。"""
    for table_name in ("kpi_snapshots", "accounts"):
        assert "warehouse_id" in Base.metadata.tables[table_name].c.keys()


def test_kpi_snapshot_has_no_generated_at_extra_column(session: Session) -> None:
    """17 §5.1 的「生成时间」就是继承来的 `created_at`，不另立第二列。

    与 `ImportSession` 同一处置（`17` §3.1 的「导入操作时间」即 `created_at`）：
    行是什么时候生成的，`created_at` 已经答了；再加一列二者可以不一致。
    """
    columns = Base.metadata.tables["kpi_snapshots"].c.keys()
    assert "created_at" in columns
    assert "generated_at" not in columns


def test_kpi_snapshot_timestamps_are_not_used_as_the_business_key() -> None:
    """业务键是 `period`（`2026-W37`）而不是时间戳 —— 与 D1「展示形状不进存储键」同一口径。

    `created_at` 是「何时算出来的」，重算会变；`period` 是「算的是哪一周」，不变。
    用前者当键，重算一次就会多出一行。
    """
    assert "period" in Base.metadata.tables["kpi_snapshots"].c.keys()
    assert Base.metadata.tables["kpi_snapshots"].c.period.type.length == 16  # type: ignore[union-attr]
