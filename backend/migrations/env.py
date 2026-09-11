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

还有一处必须在这里补的过滤（`include_object`，见 `_type_generated_checks`）——
不补的话，每个枚举列都会让 autogenerate 报一条**假**的「删除约束」。
"""
from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
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


def _type_generated_checks() -> dict[str, set[str]]:
    """表名 → 「类型自带」CHECK 约束名集合。

    `sa.Enum(create_constraint=True)`（D3 的取值约束第二层）把 CHECK 挂在**类型**上，
    而不是表上。Alembic 的检查约束比对会把这类约束从 metadata 侧排除
    （`sqla_compat._is_type_bound`，出处 SQLAlchemy #3260「不要拷贝由类型生成的 CHECK」），
    但反射侧照单全收 —— 于是库里那条真实存在的 CHECK 被判成「库里多出来的约束」，
    每次 autogenerate 都想 DROP 它，空 diff 的验收永远过不去（且会写出一条
    把取值约束删掉的迁移，直接违背 D3）。

    这里按同一口径把反射侧也排除：两侧同时忽略，比对才回到「真的有没有差异」。
    约束本身仍在库里（`ck_materials_abc_class CHECK (abc_class IN ('A','B','C'))`），
    D3 的两层约束一层不少。

    ⚠️ **代价，必须知道**：正因为两侧都看不见它，**改动枚举取值时 autogenerate 报不出差异**。
    改 `app/core/enums.py` 的取值（或增删取值）时，必须**手写**一条迁移重建该 CHECK ——
    否则 Python 侧接受了新取值、库里的 CHECK 仍按旧值拒绝写入，故障现场只会在写入时报错。
    """
    names: dict[str, set[str]] = {}
    for table in target_metadata.tables.values():
        bound = {
            str(c.name)
            for c in table.constraints
            if isinstance(c, sa.CheckConstraint) and c._type_bound and c.name
        }
        if bound:
            names[table.name] = bound
    return names


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """autogenerate 的对象过滤器。

    只做一件事：把「类型自带」的 CHECK 在**反射侧**也排除掉（理由与代价见
    `_type_generated_checks` 的 docstring）。其余对象一律不过滤 ——
    过滤器每放宽一条，空 diff 的验收就少守一块地方。
    """
    if type_ == "check_constraint" and reflected:
        table = getattr(object_, "table", None)
        # 每次现算而不缓存：测试会往 metadata 里加探针模型再移除
        # （tests/models/test_base.py），缓存会把探针的表算进去。
        bound = _type_generated_checks().get(getattr(table, "name", ""), set())
        if name in bound:
            return False
    return True


def _run_migrations(connection) -> None:
    """在给定连接上跑迁移。两个入口（应用引擎 / 调用方自带）共用同一套 configure。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。

    离线模式下拿不到反射结果，故 `include_object` 的反射分支不会触发；
    仍传入以保持两个入口的 configure 完全一致。
    """
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：默认复用应用引擎；调用方自带连接时用调用方的。

    默认路径刻意不用 `engine_from_config`：那会造出第二个引擎，丢掉 `app/core/db.py`
    在 connect 事件上挂的 PRAGMA（WAL / foreign_keys / busy_timeout）。
    迁移与应用必须跑在同一套连接配置上，否则「迁移时 FK 关着、运行时开着」
    这类差异会在数据上留下痕迹。

    `config.attributes["connection"]` 是 Alembic 的既有约定（"Sharing a Connection
    with a Series of Migration Commands"）：测试要在一个**临时库**上跑完整迁移链，
    再拿它跟模型比对（`tests/models/test_migrations.py` 的空 diff 用例）。此时
    由调用方管事务、也由调用方决定连哪个库 —— 这里就不再自造引擎。
    """
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run_migrations(supplied)
        return

    with engine.connect() as connection:
        _run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
