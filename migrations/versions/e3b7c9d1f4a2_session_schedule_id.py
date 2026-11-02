"""Link each scheduled run's chat to its schedule

Adds `sessions.schedule_id`. Every run of a schedule now opens its own chat
instead of appending to one ever-growing chat, and the scheduler keeps only the
newest run chats per schedule by this key.

Existing chats keep a NULL schedule_id and are never pruned, so no history is
deleted by the upgrade. The previous scheduler wrote its first run's chat back
onto `schedules.session_id` and appended every later run to it; those pointers
are cleared so the schedule opens a new chat per run. A chat a user chose as a
target is a normal chat, not channel 'schedule', and keeps its pointer.

Downgrade drops the column. The cleared pointers are not restored: the old code
simply opens one new chat on its next run and reuses that.

Revision ID: e3b7c9d1f4a2
Revises: d4f1a7c2e9b0
Create Date: 2026-11-02 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3b7c9d1f4a2"
down_revision: Union[str, None] = "d4f1a7c2e9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("schedule_id", sa.String(length=32), nullable=True))
    op.create_index("ix_sessions_schedule_id", "sessions", ["schedule_id"])
    op.execute(
        "UPDATE schedules SET session_id = NULL "
        "WHERE session_id IN (SELECT id FROM sessions WHERE channel = 'schedule')"
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_schedule_id", table_name="sessions")
    op.drop_column("sessions", "schedule_id")
