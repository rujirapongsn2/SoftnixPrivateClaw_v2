"""llm_models: add purpose and data_locality labels

Two display-only labels on a model. `purposes` lists what it is best at, one or
more of general|fast|reasoning|coding|long_context|multimodal. `data_locality` says
whether prompts stay on infrastructure the operator controls (local) or go to
a third-party service (external). Neither gates routing yet. Existing rows get
["general"]/external, the conservative reading: nothing is claimed to be local
until an admin says so.

Downgrade drops both columns; older code ignores them.

Revision ID: a1b2c3d4e5f7
Revises: 52fade7282c1
Create Date: 2026-10-04 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = '52fade7282c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('llm_models', sa.Column('purposes', sa.JSON(), nullable=False, server_default=sa.text('\'["general"]\'')))
    op.add_column('llm_models', sa.Column('data_locality', sa.String(16), nullable=False, server_default='external'))


def downgrade() -> None:
    op.drop_column('llm_models', 'data_locality')
    op.drop_column('llm_models', 'purposes')
