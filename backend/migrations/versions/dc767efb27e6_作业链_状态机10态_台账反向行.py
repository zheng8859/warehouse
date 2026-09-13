"""作业链_状态机10态_台账反向行

Revision ID: dc767efb27e6
Revises: 99ed4f7e48cd
Create Date: 2026-09-13 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc767efb27e6'
down_revision: Union[str, Sequence[str], None] = '99ed4f7e48cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 三处 schema 变更（design.md Migration Plan）：
    # ① job_orders.job_status 的 CHECK 由 7 值重建为 10 值，且列宽 VARCHAR(9)→VARCHAR(13)
    #    （最长取值 CANCELLED=9 → VERIFY_FAILED=13）。CHECK 由 `sa.Enum(create_constraint=True)`
    #    挂在**类型**上，env.py 的 include_object 在比对时两侧都看不见它 —— 故手写重建
    #    （见 env.py `_type_generated_checks` 的 ⚠️ 代价说明）。
    # ② ledgers 增 is_reversal（Boolean，NOT NULL，服务端默认 0）。
    # ③ ledgers 唯一约束由 (job_order_id) 放宽为 (job_order_id, is_reversal)：一单至多
    #    一正常行 + 一反向行（冲正至多一次，由状态机 VOID 终态守卫）。
    # 三处均为超集 / 加列变更：既有 7 态数据仍满足 10 值 CHECK，is_reversal 默认 0 不改语义。
    # 约束名一律包 `batch_op.f()`（= `conv()`）：批量重建会把**裸字符串**再套一层命名约定
    # （`ck_%(table_name)s_%(constraint_name)s` → `ck_job_orders_ck_job_orders_job_status`），
    # 而 `conv()` 标记「名字已经约定过」，按字面使用 —— 与既有迁移 `0559bebb5207` 同一写法。
    with op.batch_alter_table('job_orders', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_job_orders_job_status'), type_='check')
        batch_op.create_check_constraint(
            batch_op.f('ck_job_orders_job_status'),
            "status IN ('PENDING', 'PLANNED', 'CONFIRMED', 'REJECTED', 'CANCELLED', "
            "'EXECUTED', 'VERIFYING', 'VERIFIED', 'VERIFY_FAILED', 'VOID')",
        )
        batch_op.alter_column(
            'status',
            existing_type=sa.VARCHAR(length=9),
            type_=sa.VARCHAR(length=13),
            existing_nullable=False,
        )

    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_reversal', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )
        batch_op.drop_constraint(batch_op.f('uq_ledgers_job_order_id'), type_='unique')
        batch_op.create_unique_constraint(
            batch_op.f('uq_ledgers_job_order_id_reversal'), ['job_order_id', 'is_reversal']
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 反向：复原唯一约束、删 is_reversal 列、重建 7 值 CHECK 并收窄列宽。
    # 注意：若库中已有冲正产生的反向行，复原 (job_order_id) 唯一约束会因「同单两行」冲突
    # 而失败 —— 这是 schema 级回滚的固有代价（design.md 已知悉：功能开关式回滚**不删数据**，
    # 这里只复原 schema 形状，不负责把已冲正的台账重新解读成两行正常行）。
    with op.batch_alter_table('ledgers', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('uq_ledgers_job_order_id_reversal'), type_='unique')
        batch_op.create_unique_constraint(
            batch_op.f('uq_ledgers_job_order_id'), ['job_order_id']
        )
        batch_op.drop_column('is_reversal')

    with op.batch_alter_table('job_orders', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_job_orders_job_status'), type_='check')
        batch_op.create_check_constraint(
            batch_op.f('ck_job_orders_job_status'),
            "status IN ('PENDING', 'PLANNED', 'CONFIRMED', 'REJECTED', 'CANCELLED', "
            "'EXECUTED', 'VERIFIED')",
        )
        batch_op.alter_column(
            'status',
            existing_type=sa.VARCHAR(length=13),
            type_=sa.VARCHAR(length=9),
            existing_nullable=False,
        )
