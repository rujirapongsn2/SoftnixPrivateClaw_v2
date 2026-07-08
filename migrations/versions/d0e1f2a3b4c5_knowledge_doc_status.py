"""knowledge_docs.status + error — background ingestion lifecycle

Additive/backward-compatible: server_default 'ready' so every existing document
(already fully ingested) is valid immediately after upgrade.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-08 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'knowledge_docs',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='ready'),
    )
    op.add_column(
        'knowledge_docs',
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_column('knowledge_docs', 'error')
    op.drop_column('knowledge_docs', 'status')
