"""声明式基类、公共列与共享列工厂。

事实来源：17-数据模型设计 §十一（数据隔离）、§十二（留存）
          design.md D3（取值约束落两层：Python 枚举 + DB CHECK）

开发阶段决策（17 号明确把字段类型/索引/数据库实现留给开发阶段定）：

  - 主键：整型自增 `id` 作代理键；业务键（`location_code` / `order_no` / …）另加唯一约束
  - 表名：snake_case 复数（`JobOrder` → `job_orders`）
  - 不做软删除 —— 17 号用「归档不删除 + 版本化」，不引入 `is_deleted` / `deleted_at`
  - 全部实体携带 `warehouse_id` 作为过滤维度（spec `data-model`「全实体 warehouse_id 隔离」）
  - 时间列统一存 **naive UTC**：SQLite 的 DATETIME 不保存时区偏移，写入 aware 值会静默
    丢掉 offset。与其让「看着带时区、实际已丢失」的值流通，不如在入口就统一成 naive UTC
    （见 `utcnow`）。确定性要求「同样输入必得同样输出」，时间戳格式必须唯一。
  - `warehouse_id` **不加单列索引**：它几乎总是与其它列组成唯一约束或复合索引
    （如 `(warehouse_id, location_code)`），单列索引会被这些索引的前缀覆盖，
    再加一个只是白付写放大 —— 本项目是 WAL 单写者，写代价不能白花。
    热点路径需要的复合索引在各实体上单独声明（见 design.md 性能目标表）。

`enum_column` 存在的理由见其 docstring —— 这是本阶段最容易静默出错的一处。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 约束命名约定 —— 不是洁癖，是 SQLite 迁移的硬需求。
#: SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，Alembic 只能用 batch 模式
#: （建新表 → 拷数据 → 改名）。batch 模式**按名字**识别要重建的约束，
#: 匿名约束无法被寻址，改一次模型就得手写迁移。详见 design.md D8。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_T = TypeVar("_T", bound=Enum)


def utcnow() -> datetime:
    """朴素 UTC 时间戳（timezone-aware 值去掉 tzinfo）。

    见模块 docstring：SQLite 不保存时区，本项目的存储层统一用 naive UTC。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """全部 26 个实体的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BaseEntity(Base):
    """公共列载体：`id` + `warehouse_id` + `created_at`。抽象，自身不建表。"""

    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    #: 数据隔离维度；首期固定 GTJ10036。文本类型 —— 厂编码前导含字母，不得数值化。
    warehouse_id: Mapped[str] = mapped_column(String(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


def enum_column(enum_cls: type[_T], *, name: str, **kwargs: Any) -> Any:
    """枚举列工厂 —— 渲染为 `VARCHAR` + `CHECK`，且**存取值而非成员名**。

    两处非默认行为，都不是可选项：

    1. `values_callable`：`sa.Enum` 默认持久化 `member.name`。`Role.PLANNER` 的 name 是
       `PLANNER`、value 是 `planner` —— 默认行为会把 lowercase 的角色写成大写，
       与 17 §9、13 §2.2 以及既有库中的取值全部不符。`enum_column` 强制写 `.value`。
    2. `create_constraint=True` + `validate_strings=True`（design.md D3）：取值约束落两层。
       只在 Python 侧校验挡不住绕过 ORM 的写入（迁移脚本、手工 SQL、将来的批量导入），
       而本项目的全部口径都依赖取值成立。

    `name` 必填：它同时是 CHECK 约束名（配合 NAMING_CONVENTION 的 `%(constraint_name)s`），
    匿名约束在 SQLite batch 迁移下无法寻址。
    """
    if not name:
        raise ValueError("enum_column 必须显式给 name —— 它决定 CHECK 约束名")
    return mapped_column(
        sa.Enum(
            enum_cls,
            name=name,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        **kwargs,
    )
