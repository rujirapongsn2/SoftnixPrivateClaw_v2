"""skills.connector_id — link a skill to the MCP connector its instructions rely on

Lets a skill's content reference "the connected knowledge base" generically
instead of hardcoding a connector's current display name into
`mcp_{name}_{tool}` strings — the runtime resolves the connector's live,
current tool names by this id every turn, so renaming or recreating the
connector never leaves the skill's text stale. Nullable, ON DELETE SET NULL
so deleting the connector just unlinks the skill instead of failing.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-14 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('skills', sa.Column('connector_id', sa.String(length=32), nullable=True))
    op.create_index('ix_skills_connector_id', 'skills', ['connector_id'])
    op.create_foreign_key(
        'fk_skills_connector_id', 'skills', 'mcp_connectors', ['connector_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_skills_connector_id', 'skills', type_='foreignkey')
    op.drop_index('ix_skills_connector_id', table_name='skills')
    op.drop_column('skills', 'connector_id')
