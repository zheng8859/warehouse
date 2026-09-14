"""作业链_出库台账源库位可空

Revision ID: b1e5a3c7d9f2
Revises: c30f7f42599b
Create Date: 2026-09-14 14:30:00.000000

事实来源：15-入库出库移库与后验流程设计 附录A（三类台账字段矩阵）
          openspec/changes/outbound-domain/design.md D7（出库确认记录最终拣货路径）

出库确认记录的拣货路径是**巷道粒度**的 `pick_path_json`（顺路取按巷道聚合，17 §10.2），
不再是单一源库位 —— cap 增量从 `pick_path_json` 逐巷扣减（`app/cap/increment.py`）。
故 `ck_ledgers_location_columns_by_type` 的出库析取项由
`source IS NOT NULL AND target IS NULL` 放宽为 `target IS NULL`（source 可空）。

放宽是**超集变更**：旧库的出库台账（有源无目标）仍满足新 CHECK。仍钉「出库无目标」；
「给了源库位就必须是 6 位」由 `source_location_code_len6` 单独把守，与本 CHECK 正交，
不动。约束名包 `batch_op.f()`（= `conv()`）—— 批量重建会把裸字符串再套一层命名约定，
而 `conv()` 标记「名字已经约定过」，按字面使用（与既有迁移 `dc767efb27e6` 同一写法）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1e5a3c7d9f2'
down_revision: Union[str, Sequence[str], None] = 'c30f7f42599b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 重建出库析取项：source 可空（target 仍必空）。INBOUND / RELOCATE 两析取项逐字不变。
    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('ck_ledgers_location_columns_by_type'), type_='check'
        )
        batch_op.create_check_constraint(
            batch_op.f('ck_ledgers_location_columns_by_type'),
            "(ledger_type = 'INBOUND'"
            " AND source_location_code IS NULL AND target_location_code IS NOT NULL)"
            " OR (ledger_type = 'OUTBOUND'"
            " AND target_location_code IS NULL)"
            " OR (ledger_type = 'RELOCATE'"
            " AND source_location_code IS NOT NULL AND target_location_code IS NOT NULL)",
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 反向：把出库析取项收窄回「source 必非空」。代价：若库中已有「无源出库」的台账行
    # （走 `pick_path_json` 的多巷扣减），收窄会因那些行违反旧 CHECK 而失败 —— 这是
    # schema 级回滚的固有代价（与 `dc767efb27e6` 复原唯一约束的代价同一性质），
    # 这里只复原 schema 形状，不负责把无源出库重新解读成有源。
    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('ck_ledgers_location_columns_by_type'), type_='check'
        )
        batch_op.create_check_constraint(
            batch_op.f('ck_ledgers_location_columns_by_type'),
            "(ledger_type = 'INBOUND'"
            " AND source_location_code IS NULL AND target_location_code IS NOT NULL)"
            " OR (ledger_type = 'OUTBOUND'"
            " AND source_location_code IS NOT NULL AND target_location_code IS NULL)"
            " OR (ledger_type = 'RELOCATE'"
            " AND source_location_code IS NOT NULL AND target_location_code IS NOT NULL)",
        )
