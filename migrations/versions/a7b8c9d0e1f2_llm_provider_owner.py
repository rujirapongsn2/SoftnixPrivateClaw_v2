"""llm_providers.owner_id — private (BYOK) providers, per-owner name uniqueness

owner_id NULL = admin-global provider (existing behavior); non-null = a user's
own private provider. Name uniqueness moves from global to per-owner: a composite
unique (owner_id, name) plus a partial unique on name among the NULL-owner rows
(Postgres treats NULLs as distinct, so the composite index alone would not keep
global provider names unique).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-07 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_providers', sa.Column('owner_id', sa.String(length=32), nullable=True))
    op.create_index('ix_llm_providers_owner_id', 'llm_providers', ['owner_id'])
    op.create_foreign_key(
        'fk_llm_providers_owner_id', 'llm_providers', 'users',
        ['owner_id'], ['id'], ondelete='CASCADE',
    )
    # Replace the old global-name unique with per-owner uniqueness.
    op.drop_constraint('llm_providers_name_key', 'llm_providers', type_='unique')
    op.create_index(
        'ix_llm_providers_owner_name', 'llm_providers', ['owner_id', 'name'], unique=True
    )
    op.create_index(
        'ix_llm_providers_global_name', 'llm_providers', ['name'], unique=True,
        postgresql_where=sa.text('owner_id IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_llm_providers_global_name', table_name='llm_providers')
    op.drop_index('ix_llm_providers_owner_name', table_name='llm_providers')
    op.create_unique_constraint('llm_providers_name_key', 'llm_providers', ['name'])
    op.drop_constraint('fk_llm_providers_owner_id', 'llm_providers', type_='foreignkey')
    op.drop_index('ix_llm_providers_owner_id', table_name='llm_providers')
    op.drop_column('llm_providers', 'owner_id')
