"""usage_daily — per-day token rollup (day × user × model)

A pre-aggregated rollup so the Tokens Usage report never scans the unbounded
per-turn usage_records table. Maintained incrementally by UsageStore.record();
this migration also backfills it from existing raw rows in one pass.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-10 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usage_daily',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completion_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('turns', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_daily_user_id', 'usage_daily', ['user_id'])
    op.create_index('ix_usage_daily_day', 'usage_daily', ['day'])
    op.create_index('ix_usage_daily_key', 'usage_daily', ['day', 'user_id', 'model'], unique=True)

    # One-time backfill from the raw per-turn table. gen_random_uuid() is in
    # Postgres core (13+); this runs only on Postgres (prod). SQLite test DBs
    # start empty, so there's nothing to backfill there.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            INSERT INTO usage_daily (id, day, user_id, model, prompt_tokens, completion_tokens, turns)
            SELECT replace(gen_random_uuid()::text, '-', ''),
                   date_trunc('day', created_at)::date,
                   user_id,
                   COALESCE(model, ''),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(completion_tokens), 0),
                   COUNT(*)
            FROM usage_records
            GROUP BY date_trunc('day', created_at)::date, user_id, COALESCE(model, '')
            """
        )


def downgrade() -> None:
    op.drop_index('ix_usage_daily_key', table_name='usage_daily')
    op.drop_index('ix_usage_daily_day', table_name='usage_daily')
    op.drop_index('ix_usage_daily_user_id', table_name='usage_daily')
    op.drop_table('usage_daily')
