"""Add an explicit automatic fallback flag to chat models.

Revision ID: a2c3d4e5f6b7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-11 07:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2c3d4e5f6b7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_models",
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "uq_llm_models_single_fallback",
            "llm_models",
            ["is_fallback"],
            unique=True,
            postgresql_where=sa.text("is_fallback = true"),
        )
    elif dialect == "sqlite":
        op.create_index(
            "uq_llm_models_single_fallback",
            "llm_models",
            ["is_fallback"],
            unique=True,
            sqlite_where=sa.text("is_fallback = 1"),
        )


def downgrade() -> None:
    op.drop_index("uq_llm_models_single_fallback", table_name="llm_models")
    op.drop_column("llm_models", "is_fallback")
