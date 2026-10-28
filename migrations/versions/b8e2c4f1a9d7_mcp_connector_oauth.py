"""OAuth grants for per-user remote MCP connectors

Adds `mcp_connector_auth` (one encrypted grant plus refresh lease and pending
flow per connector) and `mcp_oauth_clients` (our client identity per
authorization server). The auth table's composite FK onto
mcp_connectors(id, owner_id) with a NOT NULL owner_id is what keeps OAuth off
admin-global connectors at the database level; that FK needs the new unique
constraint on mcp_connectors(id, owner_id), which the primary key already
guarantees.

Purely additive. Downgrade drops both tables (users reconnect afterwards) and
the unique constraint; existing connector rows are untouched either way.

Revision ID: b8e2c4f1a9d7
Revises: 7c1d9a3e5b24
Create Date: 2026-10-28 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e2c4f1a9d7"
down_revision: Union[str, None] = "7c1d9a3e5b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch mode: SQLite cannot ALTER TABLE ... ADD CONSTRAINT, so the table is
    # rebuilt; on Postgres this is a plain ALTER.
    with op.batch_alter_table("mcp_connectors") as batch:
        batch.create_unique_constraint("uq_connectors_id_owner", ["id", "owner_id"])
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("client_id", sa.String(length=500), nullable=False),
        sa.Column("client_secret_enc", sa.Text(), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.String(length=32), nullable=False),
        sa.Column("redirect_uri", sa.String(length=500), nullable=False),
        sa.Column("registration_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "source", "redirect_uri", name="uq_mcp_oauth_client"),
    )
    op.create_table(
        "mcp_connector_auth",
        sa.Column("connector_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("grant_enc", sa.Text(), nullable=True),
        sa.Column("failure", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refresh_lease_owner", sa.String(length=36), nullable=True),
        sa.Column("refresh_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_state_hash", sa.String(length=64), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connector_id"),
        sa.ForeignKeyConstraint(
            ["connector_id", "owner_id"],
            ["mcp_connectors.id", "mcp_connectors.owner_id"],
            ondelete="CASCADE",
            name="fk_connector_auth_connector_owner",
        ),
    )
    op.create_index("ix_connector_auth_owner", "mcp_connector_auth", ["owner_id"])
    op.create_index(
        "ix_connector_auth_pending_state", "mcp_connector_auth", ["pending_state_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_connector_auth_pending_state", table_name="mcp_connector_auth")
    op.drop_index("ix_connector_auth_owner", table_name="mcp_connector_auth")
    op.drop_table("mcp_connector_auth")
    op.drop_table("mcp_oauth_clients")
    with op.batch_alter_table("mcp_connectors") as batch:
        batch.drop_constraint("uq_connectors_id_owner", type_="unique")
