"""policy_plans table + users.plan_id + user_groups.plan_id + usage_daily.images

Adds usage-tier plans (Free/Plus/Pro/Max/Unlimited-style) that gate model
access by cost ceiling and enforce daily/per-minute quotas. Plans attach
per-user (users.plan_id) or per-group (user_groups.plan_id), both nullable and
ON DELETE SET NULL so deleting a plan is safe. usage_daily.images adds a
per-day image-generation counter for the images/day quota (image gen doesn't
emit tokens, so it isn't tracked by the existing turn/token columns).

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-07-13 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'policy_plans',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_chat_cost', sa.String(length=16), nullable=False, server_default='very_high'),
        sa.Column('allow_image', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('max_image_cost', sa.String(length=16), nullable=False, server_default='very_high'),
        sa.Column('messages_per_day', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('images_per_day', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('turns_per_minute', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.add_column('users', sa.Column('plan_id', sa.String(length=32), nullable=True))
    op.create_index('ix_users_plan_id', 'users', ['plan_id'])
    op.create_foreign_key(
        'fk_users_plan_id', 'users', 'policy_plans', ['plan_id'], ['id'], ondelete='SET NULL'
    )

    op.add_column('user_groups', sa.Column('plan_id', sa.String(length=32), nullable=True))
    op.create_index('ix_user_groups_plan_id', 'user_groups', ['plan_id'])
    op.create_foreign_key(
        'fk_user_groups_plan_id', 'user_groups', 'policy_plans', ['plan_id'], ['id'], ondelete='SET NULL'
    )

    op.add_column(
        'usage_daily',
        sa.Column('images', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('usage_daily', 'images')

    op.drop_constraint('fk_user_groups_plan_id', 'user_groups', type_='foreignkey')
    op.drop_index('ix_user_groups_plan_id', table_name='user_groups')
    op.drop_column('user_groups', 'plan_id')

    op.drop_constraint('fk_users_plan_id', 'users', type_='foreignkey')
    op.drop_index('ix_users_plan_id', table_name='users')
    op.drop_column('users', 'plan_id')

    op.drop_table('policy_plans')
