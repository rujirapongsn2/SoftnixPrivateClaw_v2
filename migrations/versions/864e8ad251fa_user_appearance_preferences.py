"""user_appearance_preferences

Revision ID: 864e8ad251fa
Revises: f8a9b0c1d2e3
Create Date: 2026-07-20 21:49:27.197383
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '864e8ad251fa'
down_revision: Union[str, None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('ui_language', sa.String(length=8), nullable=True))
    op.add_column('users', sa.Column('font_size', sa.String(length=16), nullable=True))
    op.add_column('users', sa.Column('chat_background', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'chat_background')
    op.drop_column('users', 'font_size')
    op.drop_column('users', 'ui_language')
