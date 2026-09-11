"""baseline —— 阶段二建表前的空基线

Revision ID: d7f6f1ec856a
Revises:
Create Date: 2026-09-11 14:55:28.699890

事实来源：openspec/changes/data-model-permission/design.md D8

本版本**刻意不含任何 DDL**。它存在的唯一理由是给 23 张表的那批迁移一个
`down_revision` 起点 —— 没有基线，第一条迁移的 `down_revision` 只能是 `None`，
与「库最初就是空的」这件事无从区分；有了基线，「库已经过 alembic 管理」就有了
可判定的标志（`alembic_version` 表里存在本版号）。

两条不要"顺手修正"的约定：

  - **不要往本版本里补建表语句**。23 张表按四条数据链分组，各自随模型同 commit
    落地（D8：任何模型改动必须与一份 migration 同一个 commit），这样每条迁移的
    意图都能在 diff 里读懂。堆进基线就退化成一个巨型 blob。
  - **`downgrade()` 保持 `pass`**，这是正确的而非漏写。基线之前的 schema 就是
    空的，回退到基线本来就没有表要删。Alembic 逐版本走 `down_revision` 链，
    后续版本的 `downgrade` 各自删各自的表，不存在「跳过中间版本直接删空」的路径。
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "d7f6f1ec856a"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建表前的空状态 —— 无事可做。"""
    pass


def downgrade() -> None:
    """基线之前无 schema —— 无事可做。见模块 docstring，不要改成删表。"""
    pass
