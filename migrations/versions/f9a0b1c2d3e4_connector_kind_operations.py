"""mcp_connectors.kind + operations — generic REST "api" connector type

Connectors were exclusively MCP-protocol (stdio/http). This adds a "kind"
discriminator ("mcp" default | "api") to the existing table rather than a
parallel one, plus an "operations" JSON column describing the callable REST
endpoints for api-kind connectors. Existing rows backfill to kind="mcp" with
operations left null, which is inert for the existing MCP connect path.

Revision ID: f9a0b1c2d3e4
Revises: d3e4f5a6b7c8
Create Date: 2026-08-14 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'mcp_connectors',
        sa.Column('kind', sa.String(16), nullable=False, server_default='mcp'),
    )
    op.add_column(
        'mcp_connectors',
        sa.Column('operations', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('mcp_connectors', 'operations')
    op.drop_column('mcp_connectors', 'kind')
