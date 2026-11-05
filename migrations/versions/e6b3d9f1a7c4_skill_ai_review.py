"""Skill AI review and content blocks

Adds the AI review columns to `skill_assessments` (claw/skills/ai_review.py) and its
`fingerprint`, a sha256 of the skill's text and files whatever its id. Adds
`skill_blocked_content`, the fingerprints an admin blocked, so the same content
created or imported again stays blocked.

Purely additive. Existing reviews get ai_status "off" and no fingerprint; the
scanner version bump makes the startup backfill fill the fingerprint in.
Downgrade drops the table and the columns.

Revision ID: e6b3d9f1a7c4
Revises: d4f8a2c6b1e3
Create Date: 2026-11-05 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6b3d9f1a7c4"
down_revision: Union[str, None] = "d4f8a2c6b1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    sa.Column("fingerprint", sa.String(length=64), nullable=False, server_default=""),
    sa.Column("ai_status", sa.String(length=16), nullable=False, server_default="off"),
    sa.Column("ai_sha256", sa.String(length=64), nullable=False, server_default=""),
    sa.Column("ai_version", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("ai_model", sa.String(length=200), nullable=False, server_default=""),
    sa.Column("ai_error", sa.String(length=64), nullable=False, server_default=""),
    sa.Column("ai_findings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column("ai_checked_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("skill_assessments", column)
    op.create_index("ix_skill_assessments_fingerprint", "skill_assessments", ["fingerprint"])
    op.create_table(
        "skill_blocked_content",
        sa.Column("fingerprint", sa.String(length=64), primary_key=True),
        sa.Column("skill_id", sa.String(length=32), nullable=False),
        sa.Column("blocked_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_blocked_content")
    op.drop_index("ix_skill_assessments_fingerprint", table_name="skill_assessments")
    for column in reversed(_COLUMNS):
        op.drop_column("skill_assessments", column.name)
