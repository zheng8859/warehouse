"""衔接链 5 实体的契约测试（tasks.md §3 的验证）。

事实来源：17-数据模型设计 §三 / §10.4 / §10.5 · 16-数据衔接与 cap 自维护 §3 / §6.5 / 附录 A
          openspec/changes/data-model-permission/design.md D1、D2、D4、D5、D8、D12
          spec `data-model`「不可变快照基线与 cap 引用」「乐观锁并发守卫」

**为什么 CHECK 类用例走原生 SQL**：同 `test_master_data.py` 的理由 —— D3 的取值约束落两层，
ORM 那一层（`validate_strings` 在绑定参数时抛）从没碰过 DB 的 CHECK，而绕过 ORM 的写入
正是 CHECK 存在的理由。凡验 CHECK 的用例一律用 `text()` 发原生 SQL。

**本组用例里的数字都是文档里出现过的**：cap 的 120 / 48 / 72 与 200 / 0 / 200 取自
17 §10.5 的 cap 快照示例；库存行的 010104 / 3001234 / GJP2571221 / 合格 / 40 取自
16 A.1 的示例值。不编造规模，测试里的数就能与设计文档逐字对上。
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core.concurrency import assert_lock_version, bump_lock_version
from app.core.enums import ImportStatus
from app.core.errors import StateConflict
from app.models.base import Base
from app.models.linkage import AisleCap, ImportSession, InventoryItem, Snapshot

pytestmark = pytest.mark.model

#: 首期单厂（app/core/config.py 的 warehouse_code）。
WAREHOUSE = "GTJ10036"

#: 原生 INSERT 用：created_at 无 server_default，必须显式给。
NOW = "2026-09-11 08:00:00"

#: 数据时点 `2026-09-08`（17 §10.5 的 `"2026-09-08T00:00"`、16 A.1 的 `2026-09-08 24:00`
#: 是同一个时点的两种写法）。
DATA_TIME = datetime(2026, 9, 8, 0, 0)

#: 原生 INSERT 用：SQLite 按文本存 DATETIME，直接给字符串最稳 ——
#: 传 `datetime` 会走 sqlite3 已弃用的默认适配器（Python 3.12 起告警）。
DATA_TIME_SQL = "2026-09-08 00:00:00"


# ------------------------------------------------------------------ 夹具工厂
# 按依赖链顺序建父表记录（D12）：ImportSession → Snapshot → {InventoryItem, AisleCap}。

def _import_session(
    session: Session,
    *,
    session_no: str = "IMP-20260908-01",
    status: ImportStatus = ImportStatus.DRAFT,
) -> ImportSession:
    row = ImportSession(
        warehouse_id=WAREHOUSE,
        session_no=session_no,
        import_batch_no=session_no,
        data_time=DATA_TIME,
        status=status,
    )
    session.add(row)
    session.flush()
    return row


def _snapshot(
    session: Session,
    import_session: ImportSession | None = None,
    *,
    version_no: int = 1,
) -> Snapshot:
    if import_session is None:
        import_session = _import_session(session, session_no=f"IMP-20260908-{version_no:02d}")
    row = Snapshot(
        warehouse_id=WAREHOUSE,
        snapshot_time=DATA_TIME,
        version_no=version_no,
        import_session_id=import_session.id,
    )
    session.add(row)
    session.flush()
    return row


def _item(session: Session, snapshot: Snapshot, **overrides) -> InventoryItem:
    """库存明细。默认值即 16 A.1 的示例行。"""
    fields = {
        "location_code": "010104",
        "material_code": "3001234",
        "material_name": "PET500 茉莉柚茶",
        "batch_no": "GJP2571221",
        "item_status": "合格",
        "qty": 40,
        "snapshot_time": DATA_TIME,
    }
    fields.update(overrides)
    row = InventoryItem(
        warehouse_id=WAREHOUSE, snapshot_id=snapshot.id, **fields  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


def _aisle_cap(session: Session, snapshot: Snapshot, **overrides) -> AisleCap:
    """巷道容量。默认值即 17 §10.5 示例里的巷道 01。"""
    fields = {
        "aisle_no": "01",
        "cap_total": 120,
        "cap_reserved": 48,
        "cap_usable": 72,
        "is_near_station": True,
    }
    fields.update(overrides)
    row = AisleCap(
        warehouse_id=WAREHOUSE, snapshot_id=snapshot.id, **fields  # type: ignore[arg-type]
    )
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------ 注册与建表

def test_linkage_tables_are_registered() -> None:
    """5 张表必须都进 `Base.metadata`（漏登记的表在真实库里根本不会被建）。"""
    assert {
        "import_sessions",
        "snapshots",
        "inventory_items",
        "aisle_caps",
        "cap_alerts",
    } <= set(Base.metadata.tables)


def test_no_soft_delete_columns() -> None:
    """spec `data-model`：不得引入软删除列（17 §十二用「归档不删除 + 版本化」）。"""
    for name in (
        "import_sessions",
        "snapshots",
        "inventory_items",
        "aisle_caps",
        "cap_alerts",
    ):
        cols = {c.name for c in Base.metadata.tables[name].columns}
        assert "is_deleted" not in cols
        assert "deleted_at" not in cols


def test_foreign_keys_are_enforced(foreign_keys_on: bool) -> None:
    """前置断言：本组全部外键用例的前提是 PRAGMA 真的开了（D12）。"""
    assert foreign_keys_on is True


# ------------------------------------------------------------------ 3.1 ImportSession

def test_lock_version_and_business_version_are_separate_columns() -> None:
    """spec 场景「乐观锁列不与业务版本共用」（tasks 3.1 的验证动作）。

    两列语义不同：`lock_version` 是并发守卫、对用户不可见（D1）；业务版本是
    「本次导入产出的 cap 基线版本号」（16 §3.3 的分流去向），对用户可见。
    混用的典型故障是把乐观锁当业务版本读，红线「EXECUTED 后不重复写台账」当场失效。
    """
    cols = Base.metadata.tables["import_sessions"].c
    assert {"lock_version", "snapshot_version_no"} <= set(cols.keys())
    assert isinstance(cols.lock_version.type, sa.Integer)
    assert isinstance(cols.snapshot_version_no.type, sa.Integer)


def test_lock_version_guard_works_on_import_session(session: Session) -> None:
    """`ImportSession` 必须能直接进 §1 的乐观锁守卫（16 §10.6：防止多端同时导入同一时点）。"""
    row = _import_session(session)

    assert row.lock_version == 0
    assert bump_lock_version(row, 0) == 1
    # 推进乐观锁**不得**碰到业务版本列 —— 两列是分开的。
    assert row.snapshot_version_no is None

    with pytest.raises(StateConflict):
        assert_lock_version(row, 99)


def test_business_version_is_nullable_until_baseline(session: Session) -> None:
    """业务版本在会话建基准前为空是正常状态（会话可 FAILED / DISCARDED，16 §3.1）。"""
    row = _import_session(session)
    assert row.snapshot_version_no is None

    row.snapshot_version_no = 2  # 建完基准后回填（权威值仍在 Snapshot.version_no）
    session.flush()
    session.expire_all()
    assert session.execute(
        text("SELECT snapshot_version_no FROM import_sessions WHERE id = :i"),
        {"i": row.id},
    ).scalar_one() == 2


def test_data_time_is_datetime_not_date() -> None:
    """数据时点用 DATETIME：INV 的「库存记录时间」是日期时间（16 A.1），
    存进 DATE 列会静默截断时刻。"""
    assert isinstance(Base.metadata.tables["import_sessions"].c.data_time.type, sa.DateTime)


def test_import_status_accepts_all_eight_values(session: Session) -> None:
    """8 个取值全部可写，且**存的是取值**（16 §3.1 的状态机取值域）。"""
    for status in ImportStatus:
        _import_session(session, session_no=f"IMP-{status.value}", status=status)
    session.flush()

    stored = set(
        session.execute(text("SELECT status FROM import_sessions")).scalars().all()
    )
    assert stored == {s.value for s in ImportStatus}


def test_import_status_rejected_by_python_layer(session: Session) -> None:
    """D3 第一层：`Enum(validate_strings=True)` 在绑定参数时即拒（抛 `StatementError`）。"""
    with pytest.raises(StatementError):
        _import_session(session, status="NOT_A_STATUS")  # type: ignore[arg-type]


def test_import_status_rejected_by_db_check(session: Session) -> None:
    """D3 第二层：绕过 ORM 的原生写入由 DB 的 CHECK 拒绝（tasks 3.1 的验证动作）。"""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO import_sessions "
                "(warehouse_id, session_no, import_batch_no, data_time, status, "
                " lock_version, created_at) "
                "VALUES (:w, 'IMP-X', 'B-X', :dt, 'NOT_A_STATUS', 0, :now)"
            ),
            {"w": WAREHOUSE, "dt": DATA_TIME_SQL, "now": NOW},
        )
    session.rollback()


def test_session_no_is_unique_per_warehouse(session: Session) -> None:
    """会话 ID 唯一 —— 它是回执里的 `session_id`（17 §10.4），重号即无法定位一次导入。"""
    _import_session(session, session_no="IMP-20260908-01")

    with pytest.raises(IntegrityError):
        _import_session(session, session_no="IMP-20260908-01")
    session.rollback()


def test_import_batch_no_is_not_unique(session: Session) -> None:
    """批次号刻意**不加**唯一约束：16 §3.1 允许 `FAILED → DRAFT` 重新校验后再导入，
    一次会话可产生多个批次。加唯一约束会在重试路径上误伤。
    """
    a = _import_session(session, session_no="IMP-20260908-01")
    b = _import_session(session, session_no="IMP-20260908-02")
    a.import_batch_no = b.import_batch_no = "B-20260908"
    session.flush()

    assert session.execute(
        text("SELECT count(*) FROM import_sessions WHERE import_batch_no = 'B-20260908'")
    ).scalar_one() == 2


def test_receipt_json_round_trips(session: Session) -> None:
    """收据 JSON（17 §10.4，D4 落列 `receipt_json`，含分流去向）。

    **两条读法要分开断言**：ORM 走 `JSON` 类型拿回 dict；原生 `text()` 拿到的是
    **字符串** —— JSON 的反序列化挂在类型上，绕过类型就没有它（`26` 已提示
    「SQLite 下 JSON 会以字符串读回」）。混淆这两条会让「JSON 列坏了」这类问题
    在一种读法下静默通过。
    """
    receipt = {
        "session_id": "IMP-20260908-01",
        "data_time": "2026-09-08",
        "files": [
            {"file_type": "PO", "rows": 1284, "fields_hit": "5/5", "anomalies": 0,
             "status": "PASSED"},
            {"file_type": "INV", "rows": 36920, "fields_hit": "5/5", "anomalies": 0,
             "status": "PASSED"},
        ],
    }
    row = _import_session(session)
    row.receipt_json = receipt
    session.flush()
    session.expire_all()

    # ① ORM 路径：拿回的是 dict。
    assert session.execute(
        sa.select(ImportSession.receipt_json).where(ImportSession.id == row.id)
    ).scalar_one() == receipt

    # ② 原生 SQL：拿到字符串，须自己解析。
    raw = session.execute(
        text("SELECT receipt_json FROM import_sessions WHERE id = :i"), {"i": row.id}
    ).scalar_one()
    assert isinstance(raw, str)
    assert json.loads(raw) == receipt


def test_json_columns_are_stored_canonically(session: Session) -> None:
    """D4 要求的引擎级 JSON 序列化：键有序、中文不转义（`ensure_ascii=False`）。

    读回来相等只证明能序列化；这两条决定的是**库里那串文本长什么样** ——
    用 `\\uXXXX` 存中文会让「直接看库排障」这条路失效，键序不定则同一份内容
    有两种字节表示，与「同样输入必得同样输出」的口径相抵。
    """
    row = _import_session(session)
    row.files_json = {"z_file": "库存快照", "a_file": "生产订单"}
    session.flush()

    raw = session.execute(
        text("SELECT files_json FROM import_sessions WHERE id = :i"), {"i": row.id}
    ).scalar_one()
    assert isinstance(raw, str)
    assert "库存快照" in raw, "中文被转义成了 \\uXXXX"
    assert raw.index("a_file") < raw.index("z_file"), "键未排序，字节表示不唯一"
    assert json.loads(raw) == {"z_file": "库存快照", "a_file": "生产订单"}


def test_parse_window_timestamps_exist_and_are_nullable() -> None:
    """design.md 性能目标表：`ImportSession` 记录**解析起止时间**以支撑
    「解析 ≤5min/文件」的 SLA。四个里程碑列未到达时为 NULL（不填哨兵值，
    否则解析耗时会算出负数）。"""
    cols = Base.metadata.tables["import_sessions"].c
    for name in ("validating_at", "validated_at", "imported_at", "baselined_at"):
        assert name in cols.keys()
        assert cols[name].nullable is True


# ------------------------------------------------------------------ 3.2 Snapshot

def test_new_snapshot_does_not_overwrite_old_baseline(session: Session) -> None:
    """spec 场景「新快照不覆盖旧基线」（tasks 3.2 的验证动作）。

    追加式的判据不是「表里有两行」，而是**旧行仍然带着它那一刻的值**可读 ——
    快照是权威、供追溯与回滚（17 §3.2 / §十二）。
    """
    first = _snapshot(session, version_no=1)
    first.cap_snapshot_json = {"snapshot_version": "2026-09-08T00:00",
                              "aisles": [{"aisle": "01", "total": 120}]}
    session.flush()

    second = _snapshot(session, version_no=2)
    second.cap_snapshot_json = {"snapshot_version": "2026-09-09T00:00",
                               "aisles": [{"aisle": "01", "total": 118}]}
    session.flush()
    session.expire_all()

    assert session.execute(text("SELECT count(*) FROM snapshots")).scalar_one() == 2
    old_raw = session.execute(
        text("SELECT cap_snapshot_json FROM snapshots WHERE id = :i"), {"i": first.id}
    ).scalar_one()
    old = json.loads(old_raw)  # 原生 SQL 读到的是字符串（见 test_receipt_json_round_trips）
    assert old["snapshot_version"] == "2026-09-08T00:00"
    assert old["aisles"][0]["total"] == 120


def test_snapshot_version_is_unique_per_warehouse(session: Session) -> None:
    """一个时点一个版本（16 A.4）—— 同仓库版本号不得重号。"""
    _snapshot(session, version_no=1)

    with pytest.raises(IntegrityError):
        _snapshot(session, version_no=1)
    session.rollback()


def test_snapshot_requires_existing_import_session(session: Session) -> None:
    """来源必填（17 §3.2）—— 指向不存在的会话必须被拒。"""
    session.add(
        Snapshot(
            warehouse_id=WAREHOUSE,
            snapshot_time=DATA_TIME,
            version_no=9,
            import_session_id=999999,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_snapshot_has_no_update_timestamp() -> None:
    """追加式在表形状上只剩这一条可断言的结构事实：没有可变的 `updated_at`。

    真正的写入约束（只 INSERT 不 UPDATE）属阶段四的 cap 计算层；这条用例的作用是
    让「顺手给快照加个更新时间」这个动作必须先读到这里为什么不行。
    """
    cols = {c.name for c in Base.metadata.tables["snapshots"].columns}
    assert "updated_at" not in cols


# ------------------------------------------------------------------ 3.3 InventoryItem

def test_inventory_item_unique_key_rejects_same_combination(session: Session) -> None:
    """唯一键 = 库位号 + 批号 + 料号（16 A.4「库存行标识」，tasks 3.3 的验证动作）。

    库存文件无单据号码 / 行号，这三列就是它唯一的身份。
    """
    snapshot = _snapshot(session)
    _item(session, snapshot)

    with pytest.raises(IntegrityError):
        _item(session, snapshot)  # 同库位 + 同批号 + 同料号再来一行
    session.rollback()


def test_inventory_item_same_row_in_another_snapshot_is_allowed(session: Session) -> None:
    """换一份快照，同一行库存要能再写 —— 否则快照版本化（17 §十二 的追溯与趋势）无从谈起。"""
    _item(session, _snapshot(session, version_no=1))
    _item(session, _snapshot(session, version_no=2))
    session.flush()

    assert session.execute(
        text("SELECT count(*) FROM inventory_items")
    ).scalar_one() == 2


def test_inventory_item_different_batch_is_a_different_row(session: Session) -> None:
    """同库位同料号、不同批号是两行（批号是键的一部分）。"""
    snapshot = _snapshot(session)
    _item(session, snapshot)
    _item(session, snapshot, batch_no="GJP2571222")
    session.flush()

    assert session.execute(text("SELECT count(*) FROM inventory_items")).scalar_one() == 2


def test_inventory_item_location_code_is_six_char_text(session: Session) -> None:
    """库位号按 6 位文本存取（CLAUDE.md §七）：前导 0 不丢，长度错即拒。"""
    snapshot = _snapshot(session)
    _item(session, snapshot)
    session.expire_all()

    stored = session.execute(
        text("SELECT location_code FROM inventory_items")
    ).scalar_one()
    assert stored == "010104" and stored[0] == "0" and stored[:2] == "01"

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO inventory_items "
                "(warehouse_id, snapshot_id, location_code, material_code, batch_no, "
                " item_status, qty, snapshot_time, created_at) "
                "VALUES (:w, :s, '10104', '3001234', 'GJP1', '合格', 1, :dt, :now)"
            ),
            {"w": WAREHOUSE, "s": snapshot.id, "dt": DATA_TIME_SQL, "now": NOW},
        )
    session.rollback()


def test_inventory_item_qty_must_be_positive(session: Session) -> None:
    """16 A.1 的校验规则「数量 > 0」落库 —— 0 或负数会让 cap 聚合算出比物理更大的余量。"""
    snapshot = _snapshot(session)

    for bad_qty in (0, -1):
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO inventory_items "
                    "(warehouse_id, snapshot_id, location_code, material_code, batch_no, "
                    " item_status, qty, snapshot_time, created_at) "
                    "VALUES (:w, :s, '010104', '3001234', 'GJP1', '合格', :q, :dt, :now)"
                ),
                {"w": WAREHOUSE, "s": snapshot.id, "q": bad_qty, "dt": DATA_TIME_SQL, "now": NOW},
            )
        session.rollback()


def test_inventory_item_requires_existing_snapshot(session: Session) -> None:
    """明细挂在快照上，指向不存在的快照必须被拒。"""
    session.add(
        InventoryItem(
            warehouse_id=WAREHOUSE,
            snapshot_id=999999,
            location_code="010104",
            material_code="3001234",
            batch_no="GJP2571221",
            item_status="合格",
            qty=40,
            snapshot_time=DATA_TIME,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_inventory_item_status_has_no_check(session: Session) -> None:
    """库存状态是**源数据驱动**的，不建 CHECK（design.md Open Questions / 17 §9⑪）：
    取值域以 GTJ10036 导出为准，由 `FieldMappingConfig` 归一。写死三个已知取值
    会把「导出里出现的第四种状态」变成导入失败。
    """
    snapshot = _snapshot(session)
    _item(session, snapshot, item_status="待检")
    _item(session, snapshot, item_status="报废待定", batch_no="GJP2571222")
    session.flush()

    stored = set(session.execute(text("SELECT item_status FROM inventory_items")).scalars())
    assert stored == {"待检", "报废待定"}


def test_inventory_item_production_date_is_nullable(session: Session) -> None:
    """`production_date`：17 §3.3 列了它，16 A.1 的 INV 模版已移除
    （「库区号、生产日期不再需要」）—— 保留列但可空，真实导入通常不填。

    同组的 `zone` 已随 `retire-zone-column` 删除，故本用例只剩生产日期一侧。
    """
    snapshot = _snapshot(session)
    row = _item(session, snapshot)

    assert row.production_date is None

    row.production_date = date(2026, 9, 5)
    session.flush()  # 填了也要收


def test_inventory_item_has_no_zone_column() -> None:
    """`InventoryItem.zone` 已随 `retire-zone-column` 清退，且**不得加回**。

    断言「不存在」的用例天然有**恒真**的风险，故这里盯的是**模型**而不是已建库：
    把 `zone` 加回 `InventoryItem` 时本用例必须变红（已人工核对过一次）。
    只查库抓不到这类漂移 —— 模型加回列而迁移没跟上时，库看起来完全正常，
    而 `create_all` 建的内存测试库会多出这一列。写法与 `test_no_soft_delete_columns`
    同款（列名集合断言）。
    """
    cols = {c.name for c in Base.metadata.tables["inventory_items"].columns}
    assert "zone" not in cols


def test_inventory_item_has_no_foreign_key_to_master_data() -> None:
    """库存明细**不建**指向 `locations` / `batches` / `materials` 的外键。

    快照必须能记录「源文件里有、主数据还没建」的行（16 A.5 #4 把料号匹配列为
    **校验规则**而非落库前置），而主数据中的巷道/库位又由快照派生（16 A.4）——
    建外键会形成循环依赖。这条断言把「不是忘了加」变成可执行的证据。
    """
    cols = Base.metadata.tables["inventory_items"].c
    for name in ("location_code", "material_code", "batch_no"):
        assert not cols[name].foreign_keys, f"{name} 不该有外键"
    # 唯一该有的外键是快照方向的那一条。
    assert {fk.target_fullname for fk in cols.snapshot_id.foreign_keys} == {"snapshots.id"}


# ------------------------------------------------------------------ 3.4 AisleCap

def test_aisle_cap_requires_existing_snapshot(session: Session) -> None:
    """tasks 3.4 的验证动作：`snapshot_id` 指向不存在的快照时写入被拒（D5：基线引用必填）。"""
    session.add(
        AisleCap(
            warehouse_id=WAREHOUSE,
            snapshot_id=999999,
            aisle_no="01",
            cap_total=120,
            cap_reserved=48,
            cap_usable=72,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_aisle_cap_is_one_row_per_aisle_per_snapshot(session: Session) -> None:
    """一行/巷道/快照（D5）—— 同一份快照里同巷道再来一行会让 cap 查询出现两个答案。"""
    snapshot = _snapshot(session)
    _aisle_cap(session, snapshot)

    with pytest.raises(IntegrityError):
        _aisle_cap(session, snapshot)
    session.rollback()


def test_aisle_cap_keeps_history_across_snapshots(session: Session) -> None:
    """换快照后同巷道要能再写一行 —— 容量演进可追溯（17 §十二）。"""
    _aisle_cap(session, _snapshot(session, version_no=1))
    _aisle_cap(session, _snapshot(session, version_no=2), cap_total=118, cap_usable=70)
    session.flush()

    assert session.execute(text("SELECT count(*) FROM aisle_caps")).scalar_one() == 2


def test_aisle_cap_stores_doc_metrics(session: Session) -> None:
    """17 §10.5 示例里的两组数：近站台巷道 01（120 / 48 / 72）、非近站台 21（200 / 0 / 200）。

    `cap_reserved` 仅在近站台巷道非零（D5）—— 非近站台的 0 是口径，不是缺省。
    """
    snapshot = _snapshot(session)
    near = _aisle_cap(session, snapshot)
    far = _aisle_cap(
        session, snapshot, aisle_no="21", cap_total=200, cap_reserved=0,
        cap_usable=200, is_near_station=False,
    )
    session.expire_all()

    assert (near.cap_total, near.cap_reserved, near.cap_usable) == (120, 48, 72)
    assert near.is_near_station is True
    assert (far.cap_total, far.cap_reserved, far.cap_usable) == (200, 0, 200)
    assert far.is_near_station is False


def test_aisle_cap_near_station_may_be_unknown(session: Session) -> None:
    """近站台未知时为空（同 `Aisle.is_near_station`，16 §6.1）—— NULL ≠ False。"""
    row = _aisle_cap(session, _snapshot(session), is_near_station=None)
    assert row.is_near_station is None


def test_aisle_cap_index_covers_cap_query() -> None:
    """design.md 性能目标表：cap 查询（SLA ≤100ms）需要 `(warehouse_id, snapshot_id, aisle)`
    索引。唯一约束的前缀正好是它 —— 这里把列序钉住，避免有人调序后悄悄失去索引。
    """
    (constraint,) = [
        c for c in Base.metadata.tables["aisle_caps"].constraints
        if isinstance(c, sa.UniqueConstraint)
    ]
    assert [col.name for col in constraint.columns] == [
        "warehouse_id", "snapshot_id", "aisle_no",
    ]


def test_aisle_cap_has_no_arithmetic_check(session: Session) -> None:
    """三列 cap 的口径**不落算术 CHECK**（D5）：漂移校正会以快照重算值改写同一行
    （16 §6.4），写死的等式会让校正写不进去。`updated_at` 因此是必要的。
    """
    row = _aisle_cap(session, _snapshot(session))
    first_updated_at = row.updated_at

    # 校正后不再是 120 − 48 = 72 也没关系。
    row.cap_total, row.cap_reserved, row.cap_usable = 118, 47, 71
    session.flush()
    session.expire_all()

    assert row.cap_usable == 71
    assert row.updated_at >= first_updated_at


def test_aisle_cap_has_no_foreign_key_to_aisle_master() -> None:
    """同 `InventoryItem`：不建指向 `aisles` 的外键（快照派生主数据，不能反过来要求先有）。"""
    cols = Base.metadata.tables["aisle_caps"].c
    assert not cols.aisle_no.foreign_keys
    assert {fk.target_fullname for fk in cols.snapshot_id.foreign_keys} == {"snapshots.id"}
