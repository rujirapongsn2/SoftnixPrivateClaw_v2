"""llm_models: add kind (chat|image) for the text-to-image feature

Classifies each model so image-generation models are kept out of the chat
model picker (they can't do tool calling) and offered only via the separate
/images generation path. Existing rows default to 'chat'.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-13 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'llm_models',
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='chat'),
    )


def downgrade() -> None:
    op.drop_column('llm_models', 'kind')
