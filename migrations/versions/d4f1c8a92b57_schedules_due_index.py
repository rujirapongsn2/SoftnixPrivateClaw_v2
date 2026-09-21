"""Index the normal-mode scheduler's due query.

Revision ID: d4f1c8a92b57
Revises: c7e2a9b41d63
Create Date: 2026-09-17 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d4f1c8a92b57"
down_revision: Union[str, None] = "c7e2a9b41d63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_schedules_due", "schedules", ["enabled", "next_run_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_schedules_due", table_name="schedules")
