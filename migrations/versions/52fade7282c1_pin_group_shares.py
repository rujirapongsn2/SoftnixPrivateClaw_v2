"""pin the groups group-visibility knowledge bases and blueprints are shared with

Until now a `group` knowledge base / blueprint was visible to the owner's *current* groups,
resolved live, so adding an owner to another group silently widened who could see it. Sharing is
now an explicit list of groups chosen by the owner (like a skill's shared group).

This migration keeps today's exposure exactly: every existing group-visibility item gets the
owner's current groups as its explicit list. Blueprints get a new table for that list;
knowledge bases already have one (knowledge_base_shared_groups).

Downgrade drops the blueprint table (older code derives it from the owner's groups). The
knowledge-base rows are left in place: older code treats them as extra groups, which is a
superset of itself, so nothing is lost or newly exposed.

Revision ID: 52fade7282c1
Revises: ebc5898b7099
Create Date: 2026-10-01 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '52fade7282c1'
down_revision: Union[str, None] = 'ebc5898b7099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sbot_blueprint_shared_groups',
        sa.Column('blueprint_id', sa.String(length=32), nullable=False),
        sa.Column('group_id', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['blueprint_id'], ['sbot_blueprints.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['user_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('blueprint_id', 'group_id'),
    )
    op.create_index(
        'ix_sbot_blueprint_shared_groups_group', 'sbot_blueprint_shared_groups', ['group_id']
    )
    op.execute(
        "INSERT INTO sbot_blueprint_shared_groups (blueprint_id, group_id) "
        "SELECT b.id, m.group_id FROM sbot_blueprints b "
        "JOIN user_group_members m ON m.user_id = b.owner_id "
        "WHERE b.visibility = 'group'"
    )
    op.execute(
        "INSERT INTO knowledge_base_shared_groups (kb_id, group_id) "
        "SELECT k.id, m.group_id FROM knowledge_bases k "
        "JOIN user_group_members m ON m.user_id = k.owner_id "
        "WHERE k.visibility = 'group' AND NOT EXISTS ("
        "  SELECT 1 FROM knowledge_base_shared_groups s WHERE s.kb_id = k.id AND s.group_id = m.group_id)"
    )


def downgrade() -> None:
    op.drop_index('ix_sbot_blueprint_shared_groups_group', table_name='sbot_blueprint_shared_groups')
    op.drop_table('sbot_blueprint_shared_groups')
