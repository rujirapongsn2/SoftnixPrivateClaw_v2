"""Agent Channel: list a conversation's turns, and a completion webhook per key

Adds to `agent_turns`:
- `message_preview` (first 200 characters of the message), so a caller that
  lost a turn id can list a conversation's recent turns and tell them apart;
- `webhook_status`, `webhook_attempts`, `webhook_error`, `webhook_at`: the
  delivery record of the completion webhook;
- index `ix_agent_turns_client_conversation_created` on
  (client_id, conversation_id, created_at) for the newest-first listing.

Adds to `agent_clients`: `callback_url` and `callback_secret` (encrypted with
the server secret, needed back to sign each delivery).

Purely additive; existing rows get an empty preview and no webhook. Downgrade
drops the index and the columns.

Revision ID: a8d2c6e4f1b9
Revises: f1c4a8e2b7d3
Create Date: 2026-11-03 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8d2c6e4f1b9"
down_revision: Union[str, None] = "f1c4a8e2b7d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_agent_turns_client_conversation_created"


def upgrade() -> None:
    op.add_column(
        "agent_turns", sa.Column("message_preview", sa.String(length=200), nullable=False, server_default="")
    )
    op.add_column("agent_turns", sa.Column("webhook_status", sa.String(length=16), nullable=True))
    op.add_column("agent_turns", sa.Column("webhook_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agent_turns", sa.Column("webhook_error", sa.String(length=255), nullable=True))
    op.add_column("agent_turns", sa.Column("webhook_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(_INDEX, "agent_turns", ["client_id", "conversation_id", "created_at"])
    op.add_column("agent_clients", sa.Column("callback_url", sa.Text(), nullable=True))
    op.add_column("agent_clients", sa.Column("callback_secret", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_clients", "callback_secret")
    op.drop_column("agent_clients", "callback_url")
    op.drop_index(_INDEX, table_name="agent_turns")
    op.drop_column("agent_turns", "webhook_at")
    op.drop_column("agent_turns", "webhook_error")
    op.drop_column("agent_turns", "webhook_attempts")
    op.drop_column("agent_turns", "webhook_status")
    op.drop_column("agent_turns", "message_preview")
