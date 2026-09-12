"""Correlate background missions with the working-plan steps they own.

Revision ID: b5e6f7a8b9c0
Revises: a4d5e6f7a8b9
Create Date: 2026-09-12 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5e6f7a8b9c0"
down_revision: Union[str, None] = "a4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sbot_sessions",
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("sbot_missions", sa.Column("plan_link", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sbot_missions", "plan_link")
    op.drop_column("sbot_sessions", "plan_revision")
