"""user_group_members — a user can belong to several groups

Adds a membership table and backfills it from the old single `users.group_id`. `users.group_id`
is KEPT as a mirror of the user's primary group (so a rollback or an older build still sees one
group); the application writes both. Downgrade restores users.group_id from the memberships
(the primary, position 0, wins) before dropping the table, so no assignment is lost going back.

Revision ID: ebc5898b7099
Revises: c2e31a7d9b40
Create Date: 2026-10-01 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ebc5898b7099'
down_revision: Union[str, None] = 'c2e31a7d9b40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_group_members',
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('group_id', sa.String(length=32), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'group_id'),
    )
    op.create_index('ix_user_group_members_group_id', 'user_group_members', ['group_id'])
    op.execute(
        "INSERT INTO user_group_members (user_id, group_id) "
        "SELECT id, group_id FROM users WHERE group_id IS NOT NULL"
    )


def downgrade() -> None:
    # Keep one group per user: the earliest membership (ties broken by group id).
    op.execute(
        "UPDATE users SET group_id = ("
        "  SELECT m.group_id FROM user_group_members m WHERE m.user_id = users.id "
        "  ORDER BY m.position, m.created_at, m.group_id LIMIT 1"
        ") WHERE group_id IS NULL"
    )
    op.drop_index('ix_user_group_members_group_id', table_name='user_group_members')
    op.drop_table('user_group_members')
