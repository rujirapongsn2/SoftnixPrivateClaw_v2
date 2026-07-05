"""llm_models: add cost + description for the chat model picker

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-04 22:55:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_models', sa.Column('cost', sa.String(length=16), nullable=False, server_default='medium'))
    op.add_column('llm_models', sa.Column('description', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('llm_models', 'description')
    op.drop_column('llm_models', 'cost')
