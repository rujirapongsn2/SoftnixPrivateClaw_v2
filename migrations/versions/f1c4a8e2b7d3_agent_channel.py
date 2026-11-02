"""Agent Channel: API-keyed external agents that chat as their owner

Adds `agent_clients` (keyed hash of each API key, owner, allowed connectors),
`agent_conversations` (conversation id -> chat session) and `agent_turns`
(status and reply per message, for polling and idempotent retries).

Purely additive. Downgrade drops the three tables; the chats the agents had
stay as ordinary sessions of their owners.

Revision ID: f1c4a8e2b7d3
Revises: e3b7c9d1f4a2
Create Date: 2026-11-02 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1c4a8e2b7d3"
down_revision: Union[str, None] = "e3b7c9d1f4a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_clients",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("connector_ids", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_prefix"),
    )
    op.create_index("ix_agent_clients_owner_id", "agent_clients", ["owner_id"])
    op.create_table(
        "agent_conversations",
        sa.Column("client_id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["agent_clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("client_id", "conversation_id"),
    )
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("client_id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["agent_clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_agent_turns_idempotency"),
    )
    op.create_index("ix_agent_turns_client_id", "agent_turns", ["client_id"])
    op.create_index("ix_agent_turns_created", "agent_turns", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_turns_created", table_name="agent_turns")
    op.drop_index("ix_agent_turns_client_id", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_table("agent_conversations")
    op.drop_index("ix_agent_clients_owner_id", table_name="agent_clients")
    op.drop_table("agent_clients")
