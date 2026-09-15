"""Track plan-charged chat turns separately from total turns.

Revision ID: f3a4b5c6d7e8
Revises: e8f9a0b1c2d3
Create Date: 2026-09-15 18:05:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_daily",
        sa.Column("plan_turns", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Existing rows do not record provider ownership, so they cannot be split
    # reliably. Start the new quota counter at zero; total usage remains intact
    # in `turns`, and only the current day's Plan allowance is reset once.


def downgrade() -> None:
    op.drop_column("usage_daily", "plan_turns")
