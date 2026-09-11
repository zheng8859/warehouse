"""主数据链 6 实体的契约测试（tasks.md §2 的验证）。

事实来源：17-数据模型设计 §二 / §九 · 16-数据衔接与 cap 自维护 §4.2 / §6.1 / 附录 A
          openspec/changes/data-model-permission/design.md D3、D12
          spec `data-model`「库位号按 6 位文本处理」「全实体 warehouse_id 隔离」

**为什么 CHECK 类用例走原生 SQL**：D3 把取值约束落两层（Python 枚举 + DB CHECK），
理由是「绕过 ORM 的写入（迁移脚本、手工 SQL、将来的批量导入）会带进脏值」。
用 ORM 的 `insert()` 去测，撞上的是 SQLAlchemy 那一层（`validate_strings=True`
在绑定参数时就抛），DB 层的 CHECK 从没被执行过 —— 那正是 D3 想拦的那一类写入，
测试却碰不到它。故凡是验 CHECK 的用例，一律用 `text()` 发原生 SQL。

**SQLite 下写原生 SQL 的两个注意**：`created_at` 是应用侧默认值（非 server_default），
原生 INSERT 必须自己给；类型是 DATETIME，SQLite 按文本存，直接给字符串最稳，
传 `datetime` 会走已被弃用的 sqlite3 默认适配器。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.core.enums import AbcClass
from app.models.base import Base
from app.models.master_data import (
    Aisle,
    AisleStation,
    Batch,
    Location,
    Material,
    Warehouse,
)

pytestmark = pytest.mark.model

#: 首期单厂（app/core/config.py 的 warehouse_code）。
WAREHOUSE = "GTJ10036"

#: 原生 INSERT 用：created_at 无 server_default，必须显式给。
NOW = "2026-09-11 08:00:00"


# ------------------------------------------------------------------ 夹具工厂
# 按依赖链顺序建父表记录（D12）：Warehouse → Aisle → Location / AisleStation，
# Material → Batch。不预先造空壳 —— 每个用例只建自己需要的父行。

def _warehouse(session: Session, warehouse_id: str = WAREHOUSE) -> Warehouse:
    row = Warehouse(warehouse_id=warehouse_id, name="广州顶津成品库")
    session.add(row)
    session.flush()
    return row


def _aisle(
    session: Session,
    aisle_no: str = "01",
    *,
    total_cells: int | None = 120,
    is_near_station: bool | None = True,
) -> Aisle:
    row = Aisle(
        warehouse_id=WAREHOUSE,
        aisle_no=aisle_no,
        total_cells=total_cells,
        is_near_station=is_near_station,
    )
    session.add(row)
    session.flush()
    return row


def _location(session: Session, location_code: str = "010104") -> Location:
    row = Location(
        warehouse_id=WAREHOUSE,
        location_code=location_code,
        aisle_no=location_code[:2],
        layer_column_no=location_code[2:4],
        cell_no=location_code[4:6],
    )
    session.add(row)
    session.flush()
    return row


def _material(session: Session, material_code: str = "3001234") -> Material:
    row = Material(warehouse_id=WAREHOUSE, material_code=material_code)
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------ 注册与建表

def test_master_data_tables_are_registered() -> None:
    """6 张表必须都进 `Base.metadata` —— 漏登记的表在真实库里根本不会被建。

    `app/models/__init__.py` 只登记模块，表来自各模块的实体定义；这条断言把
    「模块建了但没登记」和「登记了但表没定义」两种漏法一起挡住。
    """
    assert {
        "warehouses",
        "aisles",
        "locations",
        "aisle_stations",
        "materials",
        "batches",
    } <= set(Base.metadata.tables)


def test_no_soft_delete_columns() -> None:
    """spec `data-model`：不得引入软删除列（17 号用「归档不删除 + 版本化」）。"""
    for name in ("warehouses", "aisles", "locations", "materials", "batches"):
        cols = {c.name for c in Base.metadata.tables[name].columns}
        assert "is_deleted" not in cols
        assert "deleted_at" not in cols


# ------------------------------------------------------------------ 2.1 Warehouse

def test_warehouse_id_is_required() -> None:
    """`warehouse_id` 必填 —— 它是全部数据的过滤维度（17 §十一）。"""
    assert Base.metadata.tables["warehouses"].c.warehouse_id.nullable is False


def test_warehouse_id_is_unique(session: Session) -> None:
    """同一仓库号不得有两行。首期单厂，本表就该只有一行。"""
    _warehouse(session)

    session.add(Warehouse(warehouse_id=WAREHOUSE, name="重复的厂"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ 2.2 Aisle

def test_aisles_distinguish_near_station(session: Session) -> None:
    """近站台 / 非近站台两条记录必须可区分 —— 预留池只对近站台巷道非零。"""
    near = _aisle(session, "01", is_near_station=True)
    far = _aisle(session, "21", is_near_station=False, total_cells=200)

    assert near.is_near_station is True
    assert far.is_near_station is False
    assert (near.total_cells, far.total_cells) == (120, 200)


def test_aisle_allows_unknown_near_station(session: Session) -> None:
    """`is_near_station = NULL` 是合法状态：巷道主数据**待补充导出**（16 §6.1）。

    可空是刻意的 —— 把「未知」压成 `False` 会让近站台巷道静默退出预留池，
    正是「降级不静默」要拦的失败模式。见 master_data.py 模块 docstring 第 3 条。
    """
    row = _aisle(session, "02", is_near_station=None, total_cells=None)
    assert row.is_near_station is None


def test_aisle_no_must_be_two_chars(session: Session) -> None:
    """巷道号 = 库位号前 2 位，故长度恒为 2。`1` 与 `01` 是不同巷道，必须被拦。"""
    _warehouse(session)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO aisles (warehouse_id, aisle_no, created_at) "
                "VALUES (:w, '1', :now)"
            ),
            {"w": WAREHOUSE, "now": NOW},
        )
    session.rollback()


# ------------------------------------------------------------------ 2.3 Location

def test_location_code_keeps_leading_zero(session: Session) -> None:
    """库位号按 6 位文本存取：`010104` 读回仍是长度 6、首字符为 `0` 的字符串。

    这是 spec `data-model` 的场景「前导 0 不丢失」。列类型若是整型，`010104`
    会变成 `10104`，巷道切片 `[:2]` 得 `10` —— 全库巷道级聚合一起算错。
    """
    _warehouse(session)
    _location(session, "010104")
    session.expire_all()

    stored = session.execute(
        text("SELECT location_code FROM locations WHERE warehouse_id = :w"),
        {"w": WAREHOUSE},
    ).scalar_one()

    assert isinstance(stored, str)
    assert stored == "010104"
    assert len(stored) == 6
    assert stored[0] == "0"
    assert stored[:2] == "01"


def test_location_code_is_unique_per_warehouse(session: Session) -> None:
    """唯一键 = `(warehouse_id, location_code)`（tasks 2.3）。"""
    _warehouse(session)
    _location(session, "010104")

    session.add(
        Location(
            warehouse_id=WAREHOUSE,
            location_code="010104",
            aisle_no="01",
            layer_column_no="01",
            cell_no="04",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_location_code_prefix_must_match_aisle(session: Session) -> None:
    """编码规则落库：三个两位段必须与库位号逐位相符。

    `location_code = '010104'` 却写 `aisle_no = '21'` 的行一旦入库，
    巷道级聚合（对 `[:2]` 切片）与列上的巷道就永久不一致 —— 这类脏数据
    不会报错，只会让 cap 与集中度算错。用原生 SQL 写入以证明 DB 层拦得住。
    """
    _warehouse(session)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO locations "
                "(warehouse_id, location_code, aisle_no, layer_column_no, cell_no, created_at) "
                "VALUES (:w, '010104', '21', '01', '04', :now)"
            ),
            {"w": WAREHOUSE, "now": NOW},
        )
    session.rollback()


def test_location_code_must_be_six_chars(session: Session) -> None:
    """`211202` 的巷道是 `21`，但截成 5 位的 `21120` 不是合法库位号。"""
    _warehouse(session)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO locations "
                "(warehouse_id, location_code, aisle_no, layer_column_no, cell_no, created_at) "
                "VALUES (:w, '21120', '21', '12', '0', :now)"
            ),
            {"w": WAREHOUSE, "now": NOW},
        )
    session.rollback()


# ------------------------------------------------------------------ 2.4 AisleStation

def test_aisle_station_requires_existing_aisle(session: Session) -> None:
    """外键可建立：巷道存在则通过，不存在则被拒（`foreign_keys=ON`）。"""
    _warehouse(session)
    _aisle(session, "01")

    session.add(
        AisleStation(
            warehouse_id=WAREHOUSE,
            aisle_no="01",
            station_code="站台 A",
            distance_weight=0.9,
        )
    )
    session.flush()  # 父行在 → 通过

    session.add(
        AisleStation(
            warehouse_id=WAREHOUSE,
            aisle_no="99",  # 没有这条巷道
            station_code="站台 B",
            distance_weight=0.3,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_aisle_station_is_one_to_one(session: Session) -> None:
    """17 ER 图 `Aisle ||--|| AisleStation` —— 一条巷道恰好一个就近站台。"""
    _warehouse(session)
    _aisle(session, "01")
    session.add(
        AisleStation(
            warehouse_id=WAREHOUSE,
            aisle_no="01",
            station_code="站台 A",
            distance_weight=0.9,
        )
    )
    session.flush()

    session.add(
        AisleStation(
            warehouse_id=WAREHOUSE,
            aisle_no="01",
            station_code="站台 B",
            distance_weight=0.3,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ 2.5 Material

def test_material_abc_class_accepts_documented_values(session: Session) -> None:
    """`abc_class` 取值限于 `A` / `B` / `C`，且存的是**取值**不是成员名。"""
    for code, abc in (("3001234", AbcClass.A), ("3005678", "B"), ("3009012", "C")):
        row = Material(
            warehouse_id=WAREHOUSE, material_code=code, abc_class=abc  # type: ignore[arg-type]
        )
        session.add(row)
    session.flush()
    session.expire_all()

    stored = session.execute(
        text("SELECT abc_class FROM materials ORDER BY material_code")
    ).scalars().all()
    assert stored == ["A", "B", "C"]


def test_material_abc_class_rejected_by_python_layer(session: Session) -> None:
    """D3 的第一层：`Enum(validate_strings=True)` 在绑定参数时即拒。

    抛的是 `StatementError`（其 `__cause__` 为 `LookupError`），不是 IntegrityError ——
    这一层根本走不到数据库。
    """
    session.add(
        Material(warehouse_id=WAREHOUSE, material_code="3001234", abc_class="D")  # type: ignore[arg-type]
    )
    with pytest.raises(StatementError):
        session.flush()
    session.rollback()


def test_material_abc_class_rejected_by_db_check(session: Session) -> None:
    """D3 的第二层：绕过 ORM 的原生写入由 DB 的 CHECK 拒绝（tasks 2.5 的验证动作）。

    这一层才是 D3 存在的理由 —— 迁移脚本、手工 SQL、将来的批量导入都不经过
    SQLAlchemy 的类型校验。
    """
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO materials "
                "(warehouse_id, material_code, abc_class, created_at) "
                "VALUES (:w, '3001234', 'D', :now)"
            ),
            {"w": WAREHOUSE, "now": NOW},
        )
    session.rollback()


def test_material_abc_class_may_be_null(session: Session) -> None:
    """ABC 由成品清单聚合**自动补录**（16 A.4）—— 物料行先于它存在是正常路径。"""
    row = _material(session, "3001234")
    assert row.abc_class is None


def test_material_code_is_unique_per_warehouse(session: Session) -> None:
    _warehouse(session)
    _material(session, "3001234")

    session.add(Material(warehouse_id=WAREHOUSE, material_code="3001234"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ------------------------------------------------------------------ 2.6 Batch

def test_batch_requires_existing_material(session: Session) -> None:
    """`foreign_keys=ON` 下指向不存在的 `material_id` 必须被拒（tasks 2.6）。

    SQLite 默认**关闭**外键约束 —— 这条用例同时是「PRAGMA 真的开了」的证据，
    配 `conftest.foreign_keys_on` 的读回断言。
    """
    _warehouse(session)

    session.add(
        Batch(
            warehouse_id=WAREHOUSE,
            batch_no="GJP2571221",
            material_id=999999,  # 没有这条物料
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_batch_links_to_material(session: Session) -> None:
    """一个料号可有多个批次（17 ER 图 `Material ||--o{ Batch`）。"""
    _warehouse(session)
    material = _material(session, "3001234")

    for batch_no in ("GJP2571221", "GJP2571222"):
        session.add(
            Batch(warehouse_id=WAREHOUSE, batch_no=batch_no, material_id=material.id)
        )
    session.flush()

    linked = session.execute(
        text("SELECT count(*) FROM batches WHERE material_id = :m"),
        {"m": material.id},
    ).scalar_one()
    assert linked == 2


def test_batch_no_is_unique_per_warehouse(session: Session) -> None:
    _warehouse(session)
    material = _material(session, "3001234")
    session.add(
        Batch(warehouse_id=WAREHOUSE, batch_no="GJP2571221", material_id=material.id)
    )
    session.flush()

    session.add(
        Batch(warehouse_id=WAREHOUSE, batch_no="GJP2571221", material_id=material.id)
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_production_date_is_a_date_not_a_number(session: Session) -> None:
    """生产日期用 DATE。Excel 序列号若原样入库，FIFO 与批次因子会拿到 46000 这类数。"""
    col = Base.metadata.tables["batches"].c.production_date
    assert isinstance(col.type, sa.Date)
    assert col.nullable is True  # 16 A.2 标为选填


def test_foreign_keys_are_enforced(foreign_keys_on: bool) -> None:
    """前置断言：本组全部外键用例的前提是 PRAGMA 真的开了（D12）。"""
    assert foreign_keys_on is True
