"""sessions.consolidation_failures — persisted poison-batch counter

Memory consolidation only advances last_consolidated_seq after a usable model
response, so a window the summarizer can never process was retried forever,
throttled only by an in-memory backoff that a restart wiped. This column
persists the consecutive-unusable-response count so the window can eventually
be skipped (loudly) instead of burning a paid LLM call on every later turn.

Revision ID: a2b3c4d5e6f7
Revises: b3c4d5e6f7a8
Create Date: 2026-08-16 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("consolidation_failures", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "consolidation_failures")
