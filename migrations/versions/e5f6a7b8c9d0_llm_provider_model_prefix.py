"""llm_providers: add model_prefix so admins type raw model ids, not LiteLLM prefixes

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-07 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_providers', sa.Column('model_prefix', sa.String(length=32), nullable=False, server_default=''))
    # Backfill existing providers from their first model's id (e.g. "openrouter/x/y" -> "openrouter"),
    # so providers created before this column existed keep working without a manual fix-up.
    op.execute(
        """
        UPDATE llm_providers p
        SET model_prefix = split_part(m.model_id, '/', 1)
        FROM (
            SELECT DISTINCT ON (provider_id) provider_id, model_id
            FROM llm_models
            ORDER BY provider_id, created_at
        ) m
        WHERE m.provider_id = p.id AND p.model_prefix = ''
        """
    )


def downgrade() -> None:
    op.drop_column('llm_providers', 'model_prefix')
