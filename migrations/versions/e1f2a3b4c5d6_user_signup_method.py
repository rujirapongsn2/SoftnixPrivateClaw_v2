"""users.signup_method — how each account was created (password/google/microsoft/admin_created/dev_token)

Additive and backward-compatible: server_default 'password' so every existing
row is valid immediately after upgrade (informational only — not a permission
boundary, so an inexact default for pre-existing rows is safe).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-09 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('signup_method', sa.String(length=16), nullable=False, server_default='password'),
    )


def downgrade() -> None:
    op.drop_column('users', 'signup_method')
