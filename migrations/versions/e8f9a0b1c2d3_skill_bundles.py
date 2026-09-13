"""Immutable Agent Skill bundles."""
from alembic import op
import sqlalchemy as sa
revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('skills', sa.Column('bundle_id', sa.String(32), nullable=True))
    op.add_column('skills', sa.Column('bundle_metadata', sa.JSON(), nullable=True))
    op.create_table('skill_bundle_versions',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('skill_id', sa.String(32), sa.ForeignKey('skills.id', ondelete='CASCADE'), nullable=False),
        sa.Column('files', sa.JSON(), nullable=False), sa.Column('source', sa.JSON(), nullable=False))
    op.create_index('ix_skill_bundle_versions_skill_id', 'skill_bundle_versions', ['skill_id'])

def downgrade():
    op.drop_table('skill_bundle_versions')
    op.drop_column('skills', 'bundle_metadata')
    op.drop_column('skills', 'bundle_id')
