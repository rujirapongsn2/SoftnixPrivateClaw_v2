"""Add shared durable jobs without changing legacy artifact/mission records.

Revision ID: e6b8d1c03a72
Revises: d4f1c8a92b57
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6b8d1c03a72'
down_revision = 'd4f1c8a92b57'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_queue_locks',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False, primary_key=False),
    )
    op.create_table('agent_jobs',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('owner_id', sa.String(32), sa.ForeignKey('users.id'), nullable=False, primary_key=False),
        sa.Column('mode', sa.String(16), nullable=False, primary_key=False),
        sa.Column('session_id', sa.String(64), nullable=False, primary_key=False),
        sa.Column('locale', sa.String(8), nullable=False, primary_key=False),
        sa.Column('status', sa.String(32), nullable=False, primary_key=False),
        sa.Column('reason', sa.String(64), nullable=False, primary_key=False),
        sa.Column('spec_hash', sa.String(64), nullable=False, primary_key=False),
        sa.Column('policy', sa.JSON(), nullable=False, primary_key=False),
        sa.Column('tokens', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('reserved_tokens', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('active_seconds', sa.Float(), nullable=False, primary_key=False),
        sa.Column('sequence', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('updated_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('finished_at', sa.Float(), nullable=True, primary_key=False),
    )
    op.create_index('ix_agent_jobs_owner_id', 'agent_jobs', ['owner_id'], unique=False)
    op.create_index('ix_agent_jobs_session_id', 'agent_jobs', ['session_id'], unique=False)
    op.create_index('ix_agent_jobs_status', 'agent_jobs', ['status'], unique=False)
    op.create_table('agent_steps',
        sa.Column('job_id', sa.String(64), sa.ForeignKey('agent_jobs.id'), nullable=False, primary_key=True),
        sa.Column('id', sa.String(48), nullable=False, primary_key=True),
        sa.Column('status', sa.String(32), nullable=False, primary_key=False),
        sa.Column('spec', sa.Text(), nullable=False, primary_key=False),
        sa.Column('checkpoint', sa.Text(), nullable=False, primary_key=False),
        sa.Column('checkpoint_version', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('evidence', sa.Text(), nullable=False, primary_key=False),
        sa.Column('fence', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('worker_id', sa.String(64), nullable=False, primary_key=False),
        sa.Column('lease_until', sa.Float(), nullable=False, primary_key=False),
        sa.Column('accounted_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('next_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('waiting_since', sa.Float(), nullable=True, primary_key=False),
        sa.Column('dependency', sa.String(64), nullable=False, primary_key=False),
        sa.Column('recoveries', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('no_progress', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('probes', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('reason', sa.String(64), nullable=False, primary_key=False),
    )
    op.create_index('ix_agent_steps_lease', 'agent_steps', ['status', 'lease_until'], unique=False)
    op.create_index('ix_agent_steps_next_at', 'agent_steps', ['next_at'], unique=False)
    op.create_index('ix_agent_steps_status', 'agent_steps', ['status'], unique=False)
    op.create_table('agent_attempts',
        sa.Column('job_id', sa.String(64), sa.ForeignKey('agent_jobs.id'), nullable=False, primary_key=True),
        sa.Column('step_id', sa.String(48), nullable=False, primary_key=True),
        sa.Column('fence', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('worker_id', sa.String(64), nullable=False, primary_key=False),
        sa.Column('started_at', sa.Float(), nullable=False, primary_key=False),
        sa.Column('finished_at', sa.Float(), nullable=True, primary_key=False),
        sa.Column('status', sa.String(32), nullable=False, primary_key=False),
    )
    op.create_table('agent_resource_calls',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('job_id', sa.String(64), sa.ForeignKey('agent_jobs.id'), nullable=False, primary_key=False),
        sa.Column('step_id', sa.String(48), nullable=False, primary_key=False),
        sa.Column('fence', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('model', sa.String(255), nullable=False, primary_key=False),
        sa.Column('reserved', sa.Integer(), nullable=False, primary_key=False),
        sa.Column('actual', sa.Integer(), nullable=True, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
    )
    op.create_index('ix_agent_resource_calls_job_id', 'agent_resource_calls', ['job_id'], unique=False)
    op.create_table('agent_job_events',
        sa.Column('job_id', sa.String(64), sa.ForeignKey('agent_jobs.id'), nullable=False, primary_key=True),
        sa.Column('sequence', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('payload', sa.Text(), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
    )
    op.create_table('agent_deliveries',
        sa.Column('key', sa.String(128), nullable=False, primary_key=True),
        sa.Column('job_id', sa.String(64), sa.ForeignKey('agent_jobs.id'), nullable=False, primary_key=False),
        sa.Column('step_id', sa.String(48), nullable=False, primary_key=False),
        sa.Column('payload', sa.Text(), nullable=False, primary_key=False),
        sa.Column('status', sa.String(32), nullable=False, primary_key=False),
        sa.Column('created_at', sa.Float(), nullable=False, primary_key=False),
        sa.UniqueConstraint('job_id', 'step_id', name='uq_agent_delivery_step'),
    )
    op.create_index('ix_agent_deliveries_job_id', 'agent_deliveries', ['job_id'], unique=False)
    op.execute("INSERT INTO agent_queue_locks (id, version) VALUES (1, 0)")


def downgrade():
    bind = op.get_bind()
    retained = (
        'agent_jobs', 'agent_steps', 'agent_attempts', 'agent_resource_calls',
        'agent_job_events', 'agent_deliveries',
    )
    if any(bind.execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first() for table in retained):
        raise RuntimeError(
            'Retain durable job history during rollback; archive jobs, checkpoints, '
            'resource accounting and delivery receipts before dropping these tables'
        )
    op.drop_table('agent_deliveries')
    op.drop_table('agent_job_events')
    op.drop_table('agent_resource_calls')
    op.drop_table('agent_attempts')
    op.drop_table('agent_steps')
    op.drop_table('agent_jobs')
    op.drop_table('agent_queue_locks')
