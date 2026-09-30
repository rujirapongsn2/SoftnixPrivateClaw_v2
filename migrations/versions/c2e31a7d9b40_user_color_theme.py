"""Add a personal color theme override.

Revision ID: c2e31a7d9b40
Revises: b91d5e6f3a20
"""

from alembic import op
import sqlalchemy as sa


revision = "c2e31a7d9b40"
down_revision = "b91d5e6f3a20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("color_theme", sa.String(24), nullable=True))


def downgrade():
    op.drop_column("users", "color_theme")
