"""feedback: one rating per message

Adds `message_key` (a stable fingerprint of the rated reply) and a unique index on
(user_id, session_id, message_key) so re-rating a reply updates the row instead of
adding another. Existing rows keep message_key NULL and are not constrained
(NULLs never collide in a unique index), so they keep counting in the totals.

Downgrade drops the index and column; older code ignores them.

Revision ID: 7c1d9a3e5b24
Revises: a1b2c3d4e5f7
Create Date: 2026-10-06 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7c1d9a3e5b24"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (("feedback", "ix_feedback_message"), ("sbot_feedback", "ix_sbot_feedback_message"))


def upgrade() -> None:
    for table, index in _TABLES:
        op.add_column(table, sa.Column("message_key", sa.String(length=64), nullable=True))
        op.create_index(index, table, ["user_id", "session_id", "message_key"], unique=True)


def downgrade() -> None:
    for table, index in _TABLES:
        op.drop_index(index, table_name=table)
        op.drop_column(table, "message_key")
