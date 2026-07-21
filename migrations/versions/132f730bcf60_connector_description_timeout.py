"""connector_description_timeout

Revision ID: 132f730bcf60
Revises: 864e8ad251fa
Create Date: 2026-07-21 06:14:22.580415
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '132f730bcf60'
down_revision: Union[str, None] = '864e8ad251fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('mcp_connectors', sa.Column('description', sa.Text(), nullable=True))
    op.add_column('mcp_connectors', sa.Column('timeout_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('mcp_connectors', 'timeout_ms')
    op.drop_column('mcp_connectors', 'description')
