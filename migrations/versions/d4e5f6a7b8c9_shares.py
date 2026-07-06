"""public share links: immutable answer snapshots served via capability URL

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-06 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shares',
        sa.Column('id', sa.String(length=32), primary_key=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('session_id', sa.String(length=32), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False, server_default='Shared answer'),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('view_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_shares_token_hash', 'shares', ['token_hash'], unique=True)
    op.create_index('ix_shares_user_id', 'shares', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_shares_user_id', table_name='shares')
    op.drop_index('ix_shares_token_hash', table_name='shares')
    op.drop_table('shares')
