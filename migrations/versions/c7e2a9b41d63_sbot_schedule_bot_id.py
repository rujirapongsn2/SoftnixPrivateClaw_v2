"""Run each scheduled task as the bot that owns it, and index the due query.

Revision ID: c7e2a9b41d63
Revises: f3a4b5c6d7e8
Create Date: 2026-09-17 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c7e2a9b41d63"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sbot_schedules", sa.Column("bot_id", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "fk_sbot_schedules_bot_id", "sbot_schedules", "sbot_bots", ["bot_id"], ["id"]
    )
    op.create_index(op.f("ix_sbot_schedules_bot_id"), "sbot_schedules", ["bot_id"], unique=False)
    # Existing rows stay NULL: they were all executed by the Chief of Staff
    # before this column existed, so NULL is their true history, not a gap.
    op.create_index(
        "ix_sbot_schedules_due", "sbot_schedules", ["enabled", "next_run_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_sbot_schedules_due", table_name="sbot_schedules")
    op.drop_index(op.f("ix_sbot_schedules_bot_id"), table_name="sbot_schedules")
    op.drop_constraint("fk_sbot_schedules_bot_id", "sbot_schedules", type_="foreignkey")
    op.drop_column("sbot_schedules", "bot_id")
