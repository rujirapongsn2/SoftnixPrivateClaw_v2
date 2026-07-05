"""admin console: llm providers/models, guardrail rules, app settings, session model

Revision ID: a1b2c3d4e5f6
Revises: f4200f12107c
Create Date: 2026-07-04 21:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'f4200f12107c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('model', sa.String(length=128), nullable=True))
    op.create_index('ix_audit_kind_time', 'audit_events', ['kind', 'created_at'], unique=False)

    op.create_table(
        'llm_providers',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('api_key', sa.Text(), nullable=False),
        sa.Column('api_base', sa.String(length=500), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'llm_models',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('provider_id', sa.String(length=32), nullable=False),
        sa.Column('model_id', sa.String(length=128), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['provider_id'], ['llm_providers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_llm_models_provider', 'llm_models', ['provider_id'], unique=False)

    op.create_table(
        'guardrail_rules',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('pattern', sa.Text(), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('placeholder', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False),
        sa.Column('block_message', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('is_builtin', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
    op.drop_table('guardrail_rules')
    op.drop_index('ix_llm_models_provider', table_name='llm_models')
    op.drop_table('llm_models')
    op.drop_table('llm_providers')
    op.drop_index('ix_audit_kind_time', table_name='audit_events')
    op.drop_column('sessions', 'model')
