"""Preserve pre-admission reservations across API crashes."""
from alembic import op
import sqlalchemy as sa

revision = 'f7a92d03b614'
down_revision = 'e6b8d1c03a72'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_foreground_journals',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.String(32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('job_id', sa.String(64), nullable=True),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False))
    op.create_index('ix_agent_foreground_journals_owner_id', 'agent_foreground_journals', ['owner_id'])
    op.create_index('ix_agent_foreground_journals_status', 'agent_foreground_journals', ['status'])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM agent_foreground_journals LIMIT 1')).first():
        raise RuntimeError('Retain foreground journals during rollback; archive accounting evidence before dropping this table')
    op.drop_table('agent_foreground_journals')
