"""knowledge bases: OKF bundles, documents, and searchable chunks

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-06 09:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'knowledge_bases',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('owner_id', sa.String(length=32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('visibility', sa.String(length=16), nullable=False, server_default='private'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_knowledge_bases_owner_id', 'knowledge_bases', ['owner_id'])

    op.create_table(
        'knowledge_docs',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('kb_id', sa.String(length=32), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('concept_id', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('filename', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('mime', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chars', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('chunks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_kdocs_kb', 'knowledge_docs', ['kb_id'])

    op.create_table(
        'knowledge_chunks',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('kb_id', sa.String(length=32), sa.ForeignKey('knowledge_bases.id'), nullable=False),
        sa.Column('doc_id', sa.String(length=32), sa.ForeignKey('knowledge_docs.id'), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('title', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('text', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_kchunks_kb', 'knowledge_chunks', ['kb_id'])

    # Trigram search over chunk text — language-agnostic (handles Thai + English
    # without word segmentation or an embedding model). GIN index accelerates
    # word_similarity / ILIKE lookups.
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    op.execute(
        'CREATE INDEX ix_kchunks_text_trgm ON knowledge_chunks '
        'USING gin (text gin_trgm_ops)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_kchunks_text_trgm')
    op.drop_index('ix_kchunks_kb', table_name='knowledge_chunks')
    op.drop_table('knowledge_chunks')
    op.drop_index('ix_kdocs_kb', table_name='knowledge_docs')
    op.drop_table('knowledge_docs')
    op.drop_index('ix_knowledge_bases_owner_id', table_name='knowledge_bases')
    op.drop_table('knowledge_bases')
