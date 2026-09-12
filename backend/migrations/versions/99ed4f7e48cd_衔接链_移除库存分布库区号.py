"""衔接链_移除库存分布库区号

Revision ID: 99ed4f7e48cd
Revises: 62cdb8b54011
Create Date: 2026-09-11 22:33:54.509857

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '99ed4f7e48cd'
down_revision: Union[str, Sequence[str], None] = '62cdb8b54011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 走**原生** ALTER TABLE ... DROP COLUMN，**不包 batch_alter_table**（design.md D2）。
    # Alembic 的 SQLite 方言里 requires_recreate_in_batch() 对 add_column / create_index /
    # drop_index **之外**的操作一律返回 True —— 一旦包进 batch 模式就会整表重建：建新表、
    # 搬数据、再重建外键与那 5 列复合唯一约束。对一次纯删列，那是把「改一个 schema 字符串」
    # 升级成「一次数据迁移」，风险高于收益。
    # zone 不被主键 / 唯一约束 / 外键 / CHECK / 任何索引引用，故原生路径可用（需 SQLite ≥ 3.35，
    # 本机 3.50.4）；低于 3.35 时这里**抛错而非静默降级成重建**，迁移快速失败、不留半成品。
    op.drop_column('inventory_items', 'zone')


def downgrade() -> None:
    """Downgrade schema."""
    # 只恢复**列的形状**，不恢复数据 —— 删列即丢值，downgrade 找不回来（design.md D3）。
    # 声明这一点是为了让「回滚」不被误读为「无损撤销」：真正无损的只有 schema 形状。
    # 可接受：16 §394 已写明阶段三「四类输入全空」是预期状态，本列在任何已建库中都为空。
    # 定义与 17 改前、linkage.py 改前逐字一致（String(32) / nullable=True）。
    op.add_column(
        'inventory_items',
        sa.Column('zone', sa.String(length=32), nullable=True),
    )
