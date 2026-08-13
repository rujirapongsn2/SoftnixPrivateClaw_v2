"""usage_records.counts_as_turn — separate billable tokens from chat turns

Background work (memory consolidation) spends real tokens without the user
taking a turn. The rollup already excluded it from usage_daily.turns, but the
admin and per-user reports count usage_records rows directly, so they reported
more turns than the quota counted. Existing rows all predate background
recording, so they backfill to true.

Revision ID: d3e4f5a6b7c8
Revises: c1d2e3f4a5b6
Create Date: 2026-08-14 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'usage_records',
        sa.Column(
            'counts_as_turn',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('1' if op.get_bind().dialect.name == 'sqlite' else 'true'),
        ),
    )


def downgrade() -> None:
    op.drop_column('usage_records', 'counts_as_turn')
