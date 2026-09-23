"""Track global model health and optional provider auto-disable.

Revision ID: b91d5e6f3a20
Revises: a8c14e72d905
"""

from alembic import op
import sqlalchemy as sa


revision = "b91d5e6f3a20"
down_revision = "a8c14e72d905"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_providers", sa.Column("auto_disable_models", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("llm_models", sa.Column("health_status", sa.String(24), nullable=False, server_default="unchecked"))
    op.add_column("llm_models", sa.Column("health_reason", sa.String(40), nullable=False, server_default=""))
    op.add_column("llm_models", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("llm_models", sa.Column("health_claim_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("llm_models", sa.Column("health_auto_disabled", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("llm_models", "health_auto_disabled")
    op.drop_column("llm_models", "health_claim_until")
    op.drop_column("llm_models", "health_checked_at")
    op.drop_column("llm_models", "health_reason")
    op.drop_column("llm_models", "health_status")
    op.drop_column("llm_providers", "auto_disable_models")
