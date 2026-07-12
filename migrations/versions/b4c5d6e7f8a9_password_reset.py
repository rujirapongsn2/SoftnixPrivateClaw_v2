"""users.password_reset_sent_at + password_reset_nonce

Adds a nullable timestamp (resend cooldown) and a nullable nonce (single-use
token redemption via compare-and-swap — see UserStore.redeem_password_reset)
for the "forgot password" flow (claw/api/auth.py).

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-12 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('password_reset_sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('password_reset_nonce', sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'password_reset_nonce')
    op.drop_column('users', 'password_reset_sent_at')
