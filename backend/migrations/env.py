"""Alembic 运行环境。

事实来源：openspec/changes/data-model-permission/design.md D8

D8 的规则，逐条落在这里：

  - **真实库的唯一建库路径 = `alembic upgrade head`**；禁止 `create_all` 建真实库
    （测试库另说，见 tests/conftest.py，那是瞬时的内存库）
  - **任何模型改动必须与一份 migration 同一个 commit** —— 两条建库路径必然漂移，
    且 `autogenerate` 会产出大量假 diff
  - 连接串取自 `app.core.config.settings`，不在 alembic.ini 里写死（单一来源）

两处 SQLite 专属设置，缺一不可：

  - `render_as_batch=True`：SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，
    Alembic 只能建新表 → 拷数据 → 改名。且必须**在生成迁移时就**开 batch，
    否则 autogenerate 会写出 SQLite 执行不了的 ALTER
  - `compare_type=True`：默认不比对列类型，改了 `VARCHAR(16)` → `VARCHAR(32)`
    会被判成「无变化」，于是空 diff 的验收动作变成假绿
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context

# 必须导入 app.models 包 —— 它负责把 23 个模型模块全部登记到 Base.metadata。
# 只导入 base 的话 metadata 是空的，autogenerate 会以为「该删掉所有表」。
import app.models  # noqa: F401
from app.core.config import settings
from app.core.db import engine
from app.models.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: autogenerate 的比对基准。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：复用应用引擎。

    刻意不用 `engine_from_config`：那会造出第二个引擎，丢掉 `app/core/db.py`
    在 connect 事件上挂的 PRAGMA（WAL / foreign_keys / busy_timeout）。
    迁移与应用必须跑在同一套连接配置上，否则「迁移时 FK 关着、运行时开着」
    这类差异会在数据上留下痕迹。
    """
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
