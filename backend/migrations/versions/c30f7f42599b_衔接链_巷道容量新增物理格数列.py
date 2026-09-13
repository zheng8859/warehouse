"""衔接链_巷道容量新增物理格数列

Revision ID: c30f7f42599b
Revises: dc767efb27e6
Create Date: 2026-09-14 12:00:00.000000

事实来源：16-数据衔接与 cap 自维护 §6（cap 口径）
          openspec/changes/data-import/design.md D5（cap_physical 口径 + Migration Plan）

`aisle_caps` 新增 `cap_physical`（物理总格数，INTEGER NOT NULL）。cap_reserved 的基
从 `cap_total` 改为 `cap_physical`（固定预留带，不随占用波动），基必须落列承载 ——
只活在算式里，全量重算（`app/cap/baseline.py`）无处写物理格数。

`server_default='0'` 供存量行：加 NOT NULL 列到已有行的表，SQLite 必须给默认值才能
不报错；0 是「未重算」的占位，下一次全量重算显式覆盖。与 `dc767efb27e6` 加
`is_reversal` 同一写法（模型侧 `default=0`、迁移侧 `server_default=0`，`autogenerate`
空 diff 已实测过这条路径）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c30f7f42599b'
down_revision: Union[str, Sequence[str], None] = 'dc767efb27e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 加列是超集变更：既有行 cap_physical 落到 0（占位），其余三列 cap 值不动，
    # 语义不变（0 只是「尚未按新口径重算」）。SQLite 改表走 batch（见 linkage.py
    # 模块 docstring 的命名约定说明）。
    with op.batch_alter_table('aisle_caps', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cap_physical', sa.Integer(), nullable=False, server_default=sa.text('0'))
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 只恢复列的形状：删列即丢值，downgrade 找不回来。真正无损的只有 schema 形状。
    # 可接受：cap_physical 是新增列，降级前的库本来就没有它，旧口径（cap_total × 40%）
    # 也不读它 —— 删掉即可回到降级前的行为。
    with op.batch_alter_table('aisle_caps', schema=None) as batch_op:
        batch_op.drop_column('cap_physical')
