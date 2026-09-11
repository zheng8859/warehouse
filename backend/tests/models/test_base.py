"""声明式基类、公共列与共享列工厂的契约测试。

事实来源：17-数据模型设计 §十一（数据隔离）、§十二（留存）
          openspec/changes/data-model-permission/design.md D3（取值约束落两层）

探针模型（`_probe_*`）建在**本文件内**并随即从 `Base.metadata` 移除 ——
spec `data-model` 要求建表后**恰好 23 张表**，探针若留在 metadata 里，
真实库会多建一张表，那条断言就废了。移除放在 `finally`，断言失败也不留残留。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import String
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped
from sqlalchemy.schema import CreateTable

from app.core.enums import Role
from app.models.base import Base, BaseEntity, enum_column, utcnow

pytestmark = pytest.mark.model


# ------------------------------------------------------------------ 基类

def test_base_is_declarative_base_subclass() -> None:
    """Base 必须是 DeclarativeBase 子类（SQLAlchemy 2.0 风格，不用 1.x 的 declarative_base()）。"""
    assert issubclass(Base, DeclarativeBase)


def test_base_entity_is_abstract() -> None:
    """BaseEntity 自身不建表 —— 它是公共列的载体，不是实体。"""
    assert BaseEntity.__abstract__ is True


# ------------------------------------------------------------------ 公共列

def test_probe_model_inherits_common_columns() -> None:
    """任何继承 BaseEntity 的实体都自动带上 id / warehouse_id / created_at。"""

    class _probe_common(BaseEntity):
        __tablename__ = "_probe_common"

    try:
        table = _probe_common.__table__
        assert {"id", "warehouse_id", "created_at"} <= {c.name for c in table.columns}

        # id：整型自增代理键（17 号开发阶段决策）
        assert table.c.id.primary_key is True
        assert table.c.id.autoincrement is True

        # warehouse_id：必填，且是文本（厂编码 GTJ10036 前导非数字，不能存成整型）
        assert isinstance(table.c.warehouse_id.type, String)
        assert table.c.warehouse_id.nullable is False

        # created_at：必填，由应用侧填 UTC（见 utcnow）
        assert table.c.created_at.nullable is False
    finally:
        Base.metadata.remove(table)


def test_probe_table_not_left_in_metadata() -> None:
    """探针表不得留在 metadata 里 —— 否则建表数就不是 23。"""
    assert "_probe_common" not in Base.metadata.tables
    assert "_probe_enum" not in Base.metadata.tables


def test_utcnow_is_naive_utc() -> None:
    """时间列统一存 naive UTC。

    SQLite 的 DATETIME 不保存时区偏移，写入 aware datetime 会静默丢掉 offset。
    与其让「看起来带时区、实际已丢失」的值流通，不如在入口就统一成 naive UTC。
    """
    now = utcnow()
    assert now.tzinfo is None

    wall = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((wall - now).total_seconds()) < 5


# ------------------------------------------------------------------ 枚举列工厂（D3）

def test_enum_column_persists_values_not_names() -> None:
    """枚举列必须存**取值**而非**成员名**。

    SQLAlchemy 的 sa.Enum 默认持久化 `member.name`。`Role.PLANNER` 的 name 是
    `PLANNER`、value 是 `planner` —— 默认行为会把 lowercase 的角色写成大写，
    与 17 §9、13 §2.2 及既有库中取值全部不符。故必须给 `values_callable`。
    """

    class _probe_enum(BaseEntity):
        __tablename__ = "_probe_enum"
        role: Mapped[Role] = enum_column(Role, name="role")

    try:
        col = _probe_enum.__table__.c.role
        assert list(col.type.enums) == [m.value for m in Role]
        assert "warehouse_keeper" in col.type.enums
        assert "WAREHOUSE_KEEPER" not in col.type.enums
    finally:
        Base.metadata.remove(_probe_enum.__table__)


def test_enum_column_creates_check_constraint() -> None:
    """D3：取值约束落两层 —— Python 枚举 + DB CHECK，绕过 ORM 的写入也拦得住。

    断言走**编译后的建表 DDL** 而非 `constraint.sqltext`：后者把取值渲染成
    绑定参数（`IN (__[POSTCOMPILE_param_1])`），看不到实际取值。DDL 才是
    真正发给 SQLite 的东西，也只有它含 lowercase 取值。
    """

    class _probe_enum_ck(BaseEntity):
        __tablename__ = "_probe_enum_ck"
        role: Mapped[Role] = enum_column(Role, name="role")

    try:
        table = _probe_enum_ck.__table__
        checks = [
            c
            for c in table.constraints
            if c.__class__.__name__ == "CheckConstraint"
        ]
        assert checks, "sa.Enum 应渲染为 VARCHAR + CHECK"
        # 约束名可用于 SQLite batch 迁移寻址
        assert checks[0].name == "ck__probe_enum_ck_role"

        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        assert "warehouse_keeper" in ddl, "CHECK 必须含 lowercase 取值"
        assert "WAREHOUSE_KEEPER" not in ddl, "不得把成员名写进 DDL"
        assert "CHECK" in ddl
    finally:
        Base.metadata.remove(table)
