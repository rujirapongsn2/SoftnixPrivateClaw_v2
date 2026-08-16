"""usage_records turn-shape metrics — iterations, tool_calls, ttft_ms, duration_ms

Token counts price a turn but don't describe it: a one-shot answer and a
five-tool detour that produced the same reply look alike. These four columns
record the shape so tool-usage/latency regressions are measurable instead of
anecdotal. Backfill to 0 on existing rows (= "not measured"), which no reader
treats as a real value.

Revision ID: b3c4d5e6f7a8
Revises: f9a0b1c2d3e4
Create Date: 2026-08-16 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ('iterations', 'tool_calls', 'ttft_ms', 'duration_ms')


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            'usage_records',
            sa.Column(name, sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column('usage_records', name)
