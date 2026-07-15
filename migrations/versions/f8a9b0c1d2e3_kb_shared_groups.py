"""knowledge_base_shared_groups — additional groups a group-visibility KB is shared with

Supports a third `group` visibility level for knowledge bases (alongside the
existing `private`/`public`): a group-visibility KB is visible by default to
the owner's own current group (resolved live via User.group_id, not stored
here), plus any groups explicitly listed in this table. Both FKs cascade so
deleting a knowledge base or a group cleans up membership automatically.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-15 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'knowledge_base_shared_groups',
        sa.Column('kb_id', sa.String(length=32), nullable=False),
        sa.Column('group_id', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('kb_id', 'group_id'),
    )
    op.create_index(
        'ix_kb_shared_groups_group', 'knowledge_base_shared_groups', ['group_id']
    )


def downgrade() -> None:
    op.drop_index('ix_kb_shared_groups_group', table_name='knowledge_base_shared_groups')
    op.drop_table('knowledge_base_shared_groups')
