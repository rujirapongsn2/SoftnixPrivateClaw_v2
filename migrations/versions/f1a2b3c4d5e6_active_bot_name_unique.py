"""Prevent duplicate active bot names for an owner.

Revision ID: f1a2b3c4d5e6
Revises: f0a1b2c3d4e5
Create Date: 2026-09-10 15:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "uq_sbot_bots_owner_name_active",
            "sbot_bots",
            ["owner_id", "name"],
            unique=True,
            postgresql_where=sa.text("is_archived = false"),
        )
    elif dialect == "sqlite":
        op.create_index(
            "uq_sbot_bots_owner_name_active",
            "sbot_bots",
            ["owner_id", "name"],
            unique=True,
            sqlite_where=sa.text("is_archived = 0"),
        )
    else:
        # Supported production databases are PostgreSQL and SQLite.  On an
        # unrecognised dialect, retain the safety invariant even if it cannot
        # express a partial index; archived names then cannot be reused.
        op.create_index("uq_sbot_bots_owner_name_active", "sbot_bots", ["owner_id", "name"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_sbot_bots_owner_name_active", table_name="sbot_bots")
