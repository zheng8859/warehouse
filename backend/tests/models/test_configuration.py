"""配置与对话链 5 实体的契约测试（tasks.md §6 的验证）。

事实来源：17-数据模型设计 §七（配置实体）、§八（对话台上下文）、§九（枚举）、
          §10.1（推荐理由里的 6 因子权重示例）、§12（留存）
          14-推荐引擎与评分流程设计 §四（6 因子集合固定，不新增不删除）、
          §3.3（cap_reserved = cap_total × 近站台预留比例）、§3.5（超时释放）
          16-数据衔接与 cap 自维护 §6.1 / §353~356（阈值默认值表）、附录 A.1（字段模版）
          15-入库出库移库与后验流程设计 §8.2（四类可点击问句）、§8.3（参数化模板）
          10-AI 辅助能力（冷路径）设计 §152（四类冷路径能力）
          design.md D1（版本语义三分）、D2（枚举落点）、D3、D4（JSON 列）、D8、D12
          spec `data-model`「版本语义三分」「枚举登记范围与取值」

**取值来源**：6 因子权重取自 17 §10.1 的推荐理由示例（0.25/0.20/0.15/0.20/0.10/0.10，
**文档里唯一的一组真实权重**）；容量与阈值的默认值取自 16 §353~356 的表（40% / 18:00 /
5 / 5 / 3 / 1%）；问句取自 15 §8.2 与 §8.3。只有 `page_context` 与
`ConversationContext` 的会话号是**合成值**（文档未给样本，已在用例里注明）。
"""
from __future__ import annotations

from datetime import datetime, time
from enum import Enum

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core import enums as shared_enums
from app.core.enums import AccountStatus, FileType, Role
from app.models.base import Base
from app.models.configuration import (
    Capability,
    CapacityConfig,
    ConversationContext,
    FieldMappingConfig,
    PromptTemplate,
    WeightConfig,
)
from app.models.identity import Account
from app.models.job import JobOrder
from app.models.kpi import KpiSnapshot

pytestmark = pytest.mark.model

WAREHOUSE = "GTJ10036"
NOW = "2026-09-11 08:00:00"

#: 17 §10.1 的 6 因子权重 —— 文档里唯一一组真实权重（恰好和为 1.00，见 9.4f）。
WEIGHTS = {
    "weight_abc": 0.25,
    "weight_cap": 0.20,
    "weight_existing": 0.15,
    "weight_station": 0.20,
    "weight_batch": 0.10,
    "weight_continuity": 0.10,
}

#: 16 §353~356 的阈值默认值表。
CAPACITY_DEFAULTS = {
    "near_station_reserved_ratio": 0.40,
    "reserved_release_at": time(18, 0),
    "concentration_n": 5,
    "same_material_cross_aisle_threshold": 5,
    "same_batch_cross_aisle_threshold": 3,
    "cap_drift_alert_threshold": 0.01,
}


# ------------------------------------------------------------------ 夹具工厂

def _account(session: Session) -> Account:
    existing = session.execute(
        sa.select(Account).where(Account.username == "gtj_admin")
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = Account(
        warehouse_id=WAREHOUSE,
        username="gtj_admin",
        password_hash="$2b$12$" + "0" * 53,
        role=Role.ADMIN,
        status=AccountStatus.ACTIVE,
    )
    session.add(row)
    session.flush()
    return row


def _weights(session: Session, **overrides) -> WeightConfig:
    fields = {"warehouse_id": WAREHOUSE, "version_no": 1,
              "effective_at": datetime(2026, 9, 1, 0, 0), **WEIGHTS}
    fields.update(overrides)
    row = WeightConfig(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _capacity(session: Session, **overrides) -> CapacityConfig:
    fields = {"warehouse_id": WAREHOUSE, "version_no": 1,
              "effective_at": datetime(2026, 9, 1, 0, 0), **CAPACITY_DEFAULTS}
    fields.update(overrides)
    row = CapacityConfig(**fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _template(session: Session, **overrides) -> PromptTemplate:
    fields = {
        "template_id": "KPI_DIGEST",
        "question_text": "帮我分析本周集中度下滑的原因",
        "capability": Capability.KPI_DIGEST,
    }
    fields.update(overrides)
    row = PromptTemplate(warehouse_id=WAREHOUSE, **fields)  # type: ignore[arg-type]
    session.add(row)
    session.flush()
    return row


def _raw_insert(session: Session, table: str, **values) -> None:
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    session.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), values)


# ------------------------------------------------------------------ 6.1 WeightConfig

def test_two_weight_versions_coexist(session: Session) -> None:
    """tasks 6.1 的验证：两版本可共存（17 §七「历史版本保留可回滚」）。

    **版本共存是回滚的前提**：若新版本覆盖旧行，史上用旧权重算出的分数就再也解释不了
    —— 而「改口径伪装改善」正是 18 § 要防的反指标（同一逻辑在 KPI 侧要求保留旧口径）。
    """
    _weights(session, version_no=1)
    _weights(session, version_no=2, effective_at=datetime(2026, 10, 1, 0, 0),
             weight_abc=0.30, weight_cap=0.15)
    session.flush()

    versions = session.execute(
        text("SELECT version_no FROM weight_configs ORDER BY version_no")
    ).scalars().all()
    assert versions == [1, 2]


def test_weight_version_is_unique_per_warehouse(session: Session) -> None:
    """版本号在一个仓库内不重号 —— 否则「取 version_no 最大者」会取到两行。"""
    _weights(session, version_no=1)
    with pytest.raises(IntegrityError):
        _weights(session, version_no=1)
    session.rollback()


def test_weights_are_ratios_within_zero_and_one(session: Session) -> None:
    """每项权重是 0~1 的比例 —— 结构类 CHECK，绕过 ORM 也拦得住。

    这是**结构性**下界而不是口径：1.5 的权重不是「另一种口径」，它让得分可以超过 1，
    而 17 §10.1 的示例分值（0.86 / 0.81）显然在 [0,1] 内。**权重之和是否为 1 不作
    CHECK** —— 文档全文没有「归一」的字样，不替它定口径（已登记 9.4f）。
    """
    with pytest.raises(IntegrityError):
        _raw_insert(
            session, "weight_configs",
            warehouse_id=f"'{WAREHOUSE}'", version_no="1",
            effective_at=f"'{NOW}'", created_at=f"'{NOW}'",
            weight_abc="1.5", weight_cap="0.2", weight_existing="0.15",
            weight_station="0.2", weight_batch="0.1", weight_continuity="0.1",
        )
    session.rollback()


def test_weight_reason_list_matches_the_six_factors(session: Session) -> None:
    """列名即 14 §四 的 6 因子（ABC / 巷道 cap / 既有库位 / 站台就近 / 批次 / 连续性），
    **不落 JSON 而落六列**：`14` §135 明说因子集合「不新增、不删除」。

    集合固定时，六列能逐列加约束、能按因子查询；JSON 则把「因子少了一个」这种错误
    推迟到运行期才发现。
    """
    columns = set(Base.metadata.tables["weight_configs"].c.keys())
    expected = {f"weight_{name}" for name in
                ("abc", "cap", "existing", "station", "batch", "continuity")}
    assert expected <= columns
    assert "weights_json" not in columns


def test_weight_changer_is_optional_and_points_at_accounts(session: Session) -> None:
    """`changed_by_id` 指向账号，**可空**：首版权重由种子写入，没有变更人
    （与 `Account.created_by_id` 的自举同一处置，不编造 `system` 账号）。"""
    column = Base.metadata.tables["weight_configs"].c.changed_by_id
    assert column.nullable is True
    assert {fk.target_fullname for fk in column.foreign_keys} == {"accounts.id"}

    row = _weights(session, changed_by_id=_account(session).id)
    session.flush()
    assert row.changed_by_id is not None

    with pytest.raises(IntegrityError):
        _weights(session, version_no=9, changed_by_id=999_999)
    session.rollback()


def test_weight_config_has_business_version_columns(session: Session) -> None:
    """D1：配置型业务版本 = `version_no` + `effective_at`（**不是** `lock_version`）。

    配置的并发语义与作业单不同：配置版本是**给人看的、要能回滚的**（17 §七），
    作业单的乐观锁是防多端同时写同一行的（15 §10.6）。混用一列会让「回滚到上一版配置」
    与「检测并发冲突」变成同一件事。
    """
    columns = set(Base.metadata.tables["weight_configs"].c.keys())
    assert {"version_no", "effective_at"} <= columns
    assert "lock_version" not in columns


# ------------------------------------------------------------------ 6.3 CapacityConfig

def test_capacity_defaults_match_the_documented_table(session: Session) -> None:
    """默认值逐条取自 16 §353~356：40% / 当日 18:00 / N=5 / 同物料 5 / 同批 3 / 漂移 1%。

    **版本号与生效时间刻意不给默认值** —— 它们是这张表被读到的唯一途径（D1 的当前版本
    选取按 `version_no` + `effective_at` 走），给默认值等于允许「一版没有编号的容量配置」
    存在，而那行永远不会被任何选取逻辑命中。
    """
    row = CapacityConfig(
        warehouse_id=WAREHOUSE, version_no=1, effective_at=datetime(2026, 9, 1, 0, 0)
    )
    session.add(row)
    session.flush()

    assert row.near_station_reserved_ratio == 0.40
    assert row.reserved_release_at == time(18, 0)
    assert row.concentration_n == 5
    assert row.same_material_cross_aisle_threshold == 5
    assert row.same_batch_cross_aisle_threshold == 3
    assert row.cap_drift_alert_threshold == 0.01


def test_illegal_reserved_ratio_is_rejected(session: Session) -> None:
    """tasks 6.3 的验证：非法比例被拒（比例必须在 [0,1]，绕过 ORM 也拦得住）。

    这条 CHECK 有实际后果：`cap_reserved = cap_total × 比例`（14 §3.3），比例 > 1 会让
    预留池比巷道容量还大，`cap_usable` 变成负数 —— 一个负的可用容量会静默通过所有
    「cap 是否足够」的判断。
    """
    for bad in ("1.5", "-0.1"):
        with pytest.raises(IntegrityError):
            _raw_insert(
                session, "capacity_configs",
                warehouse_id=f"'{WAREHOUSE}'", version_no="7",
                effective_at=f"'{NOW}'", created_at=f"'{NOW}'",
                near_station_reserved_ratio=bad, reserved_release_at="'18:00:00'",
                concentration_n="5", same_material_cross_aisle_threshold="5",
                same_batch_cross_aisle_threshold="3", cap_drift_alert_threshold="0.01",
            )
        session.rollback()


def test_capacity_thresholds_must_be_positive(session: Session) -> None:
    """三项阈值（N / 同物料 / 同批）为 ≥1 的整数：阈值 0 会让任何一行都「超标」，
    系统从此天天报偏离 —— 这不是可配置的口径，是配错了。"""
    with pytest.raises(IntegrityError):
        _capacity(session, concentration_n=0)
    session.rollback()


def test_drift_threshold_is_a_positive_ratio(session: Session) -> None:
    """漂移阈值同为正比例：16 §6.4 用它判断「以快照重算值校正」的时机。"""
    with pytest.raises(IntegrityError):
        _capacity(session, cap_drift_alert_threshold=0)
    session.rollback()


def test_capacity_versions_coexist_like_weights(session: Session) -> None:
    """容量配置同样是版本化的配置（D1），两版本共存、取当前生效者。"""
    _capacity(session, version_no=1)
    row = _capacity(session, version_no=2, effective_at=datetime(2026, 10, 1, 0, 0),
                    near_station_reserved_ratio=0.30)
    session.flush()
    assert row.near_station_reserved_ratio == 0.30


def test_kpi_snapshot_may_reference_the_capacity_config_version(session: Session) -> None:
    """登记 9.4e③：18 §「历史 KpiSnapshot 保留旧口径，趋势对比须同口径」要求每一行能
    自证「按哪一版口径算的」。故 `KpiSnapshot.capacity_config_id` 指向配置行
    （**指向行而不是版本号**：行有唯一约束、可被外键看住，与 D1 拒绝时间戳串同一理由）。

    可空：`PENDING` / `ERROR` 的行还没算，自然没有口径版本。
    """
    config = _capacity(session)
    row = KpiSnapshot(warehouse_id=WAREHOUSE, period="2026-W37", capacity_config_id=config.id)
    session.add(row)
    session.flush()
    assert row.capacity_config_id == config.id

    with pytest.raises(IntegrityError):
        session.add(KpiSnapshot(
            warehouse_id=WAREHOUSE, period="2026-W38", capacity_config_id=999_999
        ))
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ 6.4 FieldMappingConfig

def test_field_mapping_round_trips(session: Session) -> None:
    """tasks 6.4 的验证：字段映射可读写（结构见 16 附录 A.1 的表头）。"""
    mapping = {
        "warehouse_no": {"target": "warehouse_id", "required": True,
                         "rule": "须与单厂编码一致"},
        "location_code": {"target": "location_code", "required": True,
                          "rule": "6 位且可切片出巷道"},
    }
    row = FieldMappingConfig(
        warehouse_id=WAREHOUSE, plant_code=WAREHOUSE, file_type=FileType.INV,
        mapping_json=mapping,
    )
    session.add(row)
    session.flush()
    session.expire_all()

    assert session.get(FieldMappingConfig, row.id).mapping_json == mapping  # type: ignore[union-attr]


def test_one_mapping_per_plant_and_file_type(session: Session) -> None:
    """一台设备一套映射：`(plant_code, file_type)` 唯一 —— 三类文件各有模版
    （16 附录 A.1/A.2/A.3），同一类型只能有一份内置映射，否则导入时取哪份都说不清。"""
    for file_type in FileType:
        session.add(FieldMappingConfig(
            warehouse_id=WAREHOUSE, plant_code=WAREHOUSE, file_type=file_type,
            mapping_json={},
        ))
    session.flush()

    with pytest.raises(IntegrityError):
        session.add(FieldMappingConfig(
            warehouse_id=WAREHOUSE, plant_code=WAREHOUSE, file_type=FileType.INV,
            mapping_json={},
        ))
        session.flush()
    session.rollback()


def test_field_mapping_has_no_version_columns(session: Session) -> None:
    """17 §七 把字段映射列为**内置**（首期 GTJ10036），不给版本号 / 生效时间 / 变更人
    —— 其余三张配置表有，它没有，这不是遗漏。"""
    columns = set(Base.metadata.tables["field_mapping_configs"].c.keys())
    assert "mapping_json" in columns
    assert not ({"version_no", "effective_at", "changed_by_id"} & columns)


# ------------------------------------------------------------------ 6.5 PromptTemplate

def test_capability_is_an_entity_local_domain(session: Session) -> None:
    """tasks 6.5 的验证：能力类型是**局部值域**，不在共享枚举登记表（D2）。"""
    assert Capability.__module__ == "app.models.configuration"
    assert not hasattr(shared_enums, "Capability")


def test_capability_covers_the_four_cold_path_capabilities(session: Session) -> None:
    """四个取值 = 10 §152 的四类冷路径能力、15 §8.2 的四条可点击问句所映射的能力。"""
    assert {c.value for c in Capability} == {"KPI 解读", "偏离归因", "权重调优", "移库方案"}


def test_template_id_is_unique_and_enabled_by_default(session: Session) -> None:
    """模板 ID 唯一；新增模板默认**启用**（15 §8.5「新增一条问句即新增一条映射，
    无需改代码」—— 新增即生效，否则"加了问句但点不到"要靠人记得回来开开关）。"""
    row = _template(session)
    assert row.enabled is True

    with pytest.raises(IntegrityError):
        _template(session)
    session.rollback()


def test_parameterised_template_keeps_its_placeholders(session: Session) -> None:
    """15 §8.3：带 `{}` 的语句是参数化模板，点击后先弹参数选择器。

    占位符既在文本里也在 `params_json` 里：前者是渲染源，后者是选择器的取值来源
    （15 §8.3 第 2 步「弹出物料选择器」）。两处都要，是因为从文本里反解占位符
    需要一套正则约定，而选择器要的是「参数名 → 候选来源」的映射。
    """
    row = _template(
        session, template_id="AISLE_WHY", capability=Capability.DEVIATION_ATTRIBUTION,
        question_text="为什么 {物料} 跨了这么多巷道？", params_json=["物料"],
    )
    session.flush()
    assert "{物料}" in row.question_text
    assert row.params_json == ["物料"]


def test_template_capability_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：能力拼错 → 该模板永远不会被冷路径调用（而它看起来是启用的）。"""
    with pytest.raises(IntegrityError):
        _raw_insert(
            session, "prompt_templates",
            warehouse_id=f"'{WAREHOUSE}'", template_id="'BROKEN'",
            question_text="'x'", capability="'KPI解读'", enabled="1",
            created_at=f"'{NOW}'",
        )
    session.rollback()


# ------------------------------------------------------------------ 6.6 ConversationContext

def test_conversation_binds_to_a_job_order_and_an_account(session: Session) -> None:
    """tasks 6.6 的验证：与 `JobOrder` 的外键可建立；用户指向账号（17 §八「用户」）。"""
    job = JobOrder(
        warehouse_id=WAREHOUSE, order_no="3573743144K55G", line_no="10",
        job_type="INBOUND", material_code="3001234", qty=40, status="PENDING",
    )
    session.add(job)
    session.flush()

    row = ConversationContext(
        warehouse_id=WAREHOUSE, session_no="CONV-20260911-01",
        account_id=_account(session).id, job_order_id=job.id,
        page_context="relocate",  # 合成值：文档未给页面上下文样本
        expires_at=datetime(2026, 9, 11, 16, 0),
    )
    session.add(row)
    session.flush()

    for column in ("account_id", "job_order_id"):
        targets = {
            fk.target_fullname
            for fk in Base.metadata.tables["conversation_contexts"].c[column].foreign_keys
        }
        assert targets in ({"accounts.id"}, {"job_orders.id"}), f"{column} 指向了 {targets}"


def test_conversation_carries_the_pending_write_context(session: Session) -> None:
    """17 §八 的「待确认的写操作（确认卡）上下文」—— 它是「审批中断后恢复」的载体。

    **不建确认卡实体**：确认卡是**前端交互态**（15 §7.2），落库的只有它的上下文，
    否则会与 `JobOrder` 上的 `disposition` / `confirm_card_digest` 形成两套处置记录
    （红线「台账只有一套」的同一条道理）。
    """
    row = ConversationContext(
        warehouse_id=WAREHOUSE, session_no="CONV-20260911-02",
        account_id=_account(session).id,
        pending_write_json={"action": "confirm", "job_order_id": 1, "card": {}},
        expires_at=datetime(2026, 9, 11, 16, 0),
    )
    session.add(row)
    session.flush()
    assert row.pending_write_json["action"] == "confirm"


def test_conversation_expires_and_is_not_a_long_term_memory(session: Session) -> None:
    """17 §八 / §12：短期上下文，过期时间**必填**（「会话结束或超时即清理」）。

    必填而不是「为空即永不过期」：本产品明说不做长期记忆（自主 Agent 的反面），
    一个没有过期时间的会话行会永久留下用户的问句原文。
    """
    assert Base.metadata.tables["conversation_contexts"].c.expires_at.nullable is False

    with pytest.raises(IntegrityError):
        session.add(ConversationContext(
            warehouse_id=WAREHOUSE, session_no="CONV-20260911-03",
            account_id=999_999, expires_at=datetime(2026, 9, 11, 16, 0),
        ))
        session.flush()
    session.rollback()


def test_conversation_session_no_is_unique_per_warehouse(session: Session) -> None:
    """会话号是业务键（17 §八「会话 ID」）。"""
    account_id = _account(session).id
    for session_no in ("CONV-20260911-04", "CONV-20260911-04"):
        session.add(ConversationContext(
            warehouse_id=WAREHOUSE, session_no=session_no, account_id=account_id,
            expires_at=datetime(2026, 9, 11, 16, 0),
        ))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ 登记范围（D2）

def test_configuration_local_domain_is_not_in_the_shared_registry() -> None:
    """能力值域与 KPI 状态、方案类型、告警类型同为实体局部值域 —— 不进 11 个共享枚举。

    本阶段的第 6 处局部值域（D2 逐条列了四处、18 §3.1 的 KPI 状态是第五处），
    判据仍是同一条：只在本实体内有意义的取值不占共享登记表的名额。
    """
    local = [
        cls
        for cls in (Capability,)
        if isinstance(cls, type) and issubclass(cls, Enum)
    ]
    assert local, "至少应有一个局部值域参与本断言"
    for cls in local:
        assert cls.__module__ == "app.models.configuration"
        assert not hasattr(shared_enums, cls.__name__)


def test_config_entities_carry_the_isolation_column() -> None:
    """§十一：全部实体携带 `warehouse_id`。"""
    for table in (
        "weight_configs", "capacity_configs", "field_mapping_configs",
        "prompt_templates", "conversation_contexts",
    ):
        assert "warehouse_id" in Base.metadata.tables[table].c.keys()


def test_config_tables_do_not_carry_row_locks() -> None:
    """这五张表都**不带** `lock_version`：D1 把乐观锁限定给 `JobOrder` / `ImportSession`
    两个**并发写热点**。给配置表加上它，等于宣称「两个管理员同时改配置」是本阶段要解决的
    问题 —— 而配置的并发语义是版本并存 + 选生效版本（D1），不是互斥。
    """
    for table in Base.metadata.tables.values():
        if table.name in (
            "weight_configs", "capacity_configs", "field_mapping_configs",
            "prompt_templates", "conversation_contexts",
        ):
            assert "lock_version" not in table.c.keys()
