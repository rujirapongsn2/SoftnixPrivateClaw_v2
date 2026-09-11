"""Add private/group/public skill visibility; existing skills stay private."""
from alembic import op
import sqlalchemy as sa

revision = "a3d4e5f6b7c8"
down_revision = "a2c3d4e5f6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("skills", sa.Column("visibility", sa.String(16), nullable=False, server_default="private"))


def downgrade():
    op.drop_column("skills", "visibility")
