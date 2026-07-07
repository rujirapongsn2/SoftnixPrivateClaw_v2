"""user_groups table + users.group_id (organizational grouping, no policy meaning)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-07 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_groups',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.add_column('users', sa.Column('group_id', sa.String(length=32), nullable=True))
    op.create_index('ix_users_group_id', 'users', ['group_id'])
    op.create_foreign_key(
        'fk_users_group_id', 'users', 'user_groups', ['group_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_users_group_id', 'users', type_='foreignkey')
    op.drop_index('ix_users_group_id', table_name='users')
    op.drop_column('users', 'group_id')
    op.drop_table('user_groups')
