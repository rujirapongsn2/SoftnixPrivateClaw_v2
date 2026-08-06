"""mcp_connectors.user_id -> owner_id — admin-global "Pre-built Connectors"

owner_id NULL = admin-global connector (new, shared by every user); non-null =
a user's own private connector (existing behavior, renamed column only — no
data loss). Name uniqueness moves from per-user to per-owner: a composite
unique (owner_id, name) plus a partial unique on name among the NULL-owner
rows (Postgres treats NULLs as distinct, so the composite index alone would
not keep global connector names unique). Mirrors
migrations/versions/a7b8c9d0e1f2_llm_provider_owner.py.

Revision ID: c1d2e3f4a5b6
Revises: 132f730bcf60
Create Date: 2026-08-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = '132f730bcf60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_connectors_user_name', table_name='mcp_connectors')
    op.drop_index(op.f('ix_mcp_connectors_user_id'), table_name='mcp_connectors')
    op.drop_constraint('mcp_connectors_user_id_fkey', 'mcp_connectors', type_='foreignkey')

    op.alter_column('mcp_connectors', 'user_id', new_column_name='owner_id', nullable=True)

    op.create_foreign_key(
        'fk_mcp_connectors_owner_id', 'mcp_connectors', 'users',
        ['owner_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index('ix_mcp_connectors_owner_id', 'mcp_connectors', ['owner_id'])
    op.create_index(
        'ix_connectors_owner_name', 'mcp_connectors', ['owner_id', 'name'], unique=True
    )
    op.create_index(
        'ix_connectors_global_name', 'mcp_connectors', ['name'], unique=True,
        postgresql_where=sa.text('owner_id IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_connectors_global_name', table_name='mcp_connectors')
    op.drop_index('ix_connectors_owner_name', table_name='mcp_connectors')
    op.drop_index('ix_mcp_connectors_owner_id', table_name='mcp_connectors')
    op.drop_constraint('fk_mcp_connectors_owner_id', 'mcp_connectors', type_='foreignkey')

    # Existing global (owner_id NULL) rows have no user to fall back to — they
    # would violate the restored NOT NULL constraint. Rather than silently
    # deleting them, abort so an operator must consciously decide what to do
    # with them first (reassign to a user, export, or explicitly delete).
    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT count(*) FROM mcp_connectors WHERE owner_id IS NULL")
    ).scalar()
    if orphaned:
        raise RuntimeError(
            f"Cannot downgrade past c1d2e3f4a5b6: {orphaned} admin-global "
            "connector(s) (owner_id IS NULL) exist and have no user to fall "
            "back to under the restored NOT NULL constraint. Reassign or "
            "delete them manually, then retry the downgrade."
        )
    op.alter_column('mcp_connectors', 'owner_id', new_column_name='user_id', nullable=False)

    op.create_foreign_key(
        'mcp_connectors_user_id_fkey', 'mcp_connectors', 'users', ['user_id'], ['id'],
    )
    op.create_index(op.f('ix_mcp_connectors_user_id'), 'mcp_connectors', ['user_id'], unique=False)
    op.create_index('ix_connectors_user_name', 'mcp_connectors', ['user_id', 'name'], unique=True)
