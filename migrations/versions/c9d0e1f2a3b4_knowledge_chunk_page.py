"""knowledge_chunks.page — 1-based source page for citations (PDF)

Additive and backward-compatible: nullable, so every existing chunk keeps
page=NULL and retrieval is unaffected (page enriches citations only).

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-08 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('knowledge_chunks', sa.Column('page', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('knowledge_chunks', 'page')
