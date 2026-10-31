"""Workspace file text index for searching the Files view by content

Adds `workspace_file_texts`: the extracted text of a user's workspace files,
one row per file, rebuilt from disk. On Postgres a GIN trigram index on the
text serves ILIKE lookups (language-agnostic, so Thai works without word
segmentation), the same approach as knowledge_chunks.

Purely additive and derived: downgrade drops the table, and the next visit to
Files rebuilds it.

Revision ID: d4f1a7c2e9b0
Revises: b8e2c4f1a9d7
Create Date: 2026-10-31 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f1a7c2e9b0"
down_revision: Union[str, None] = "b8e2c4f1a9d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_file_texts",
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("path_hash", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_id", "mode", "path_hash"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX ix_workspace_file_texts_text_trgm ON workspace_file_texts "
            "USING gin (text gin_trgm_ops)"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_workspace_file_texts_text_trgm")
    op.drop_table("workspace_file_texts")
