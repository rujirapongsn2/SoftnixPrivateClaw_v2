"""Add queryable Knowledge datasets.

Revision ID: a8c14e72d905
Revises: f7a92d03b614
"""

from alembic import op
import sqlalchemy as sa


revision = "a8c14e72d905"
down_revision = "f7a92d03b614"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_bases",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="general"),
    )
    op.add_column("knowledge_docs", sa.Column("dataset_path", sa.String(length=512), nullable=True))
    op.add_column("knowledge_docs", sa.Column("dataset_schema", sa.JSON(), nullable=True))
    op.add_column(
        "knowledge_docs",
        sa.Column("dataset_rows", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("knowledge_docs", "dataset_rows")
    op.drop_column("knowledge_docs", "dataset_schema")
    op.drop_column("knowledge_docs", "dataset_path")
    op.drop_column("knowledge_bases", "kind")
