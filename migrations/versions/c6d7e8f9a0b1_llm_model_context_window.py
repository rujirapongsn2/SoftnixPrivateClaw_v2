"""llm_models: add context_window override

The agent loop sizes its prompt-compaction ceiling from the model's input
window, looked up in LiteLLM's bundled model table. That table doesn't know
every model an operator can configure (private gateways, brand-new
checkpoints), and those silently fall back to a conservative default. This
column lets an admin state the real window per model. NULL = fall back to the
lookup, which is what every existing row wants.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-29 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_models', sa.Column('context_window', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('llm_models', 'context_window')
