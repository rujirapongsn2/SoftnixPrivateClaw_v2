"""Skill security review: stored assessments and admin decisions

Adds `skill_assessments`: the rule-based review of a skill's current version
(claw/skills/assessment.py), keyed by a sha256 of the content and scanner
version, with findings mapped to OWASP Agentic Skills Top 10 ids (AST01-AST10).
Adds `skill_security_decisions`: admin allow/block decisions and owner
requests for an exception. The share policy itself is an `app_settings` row.

Purely additive; existing skills are reviewed by the startup backfill.
Downgrade drops both tables.

Revision ID: c3e7b1d9a4f2
Revises: a8d2c6e4f1b9
Create Date: 2026-11-04 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3e7b1d9a4f2"
down_revision: Union[str, None] = "a8d2c6e4f1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_assessments",
        sa.Column("skill_id", sa.String(length=32), sa.ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("scanner_version", sa.Integer(), nullable=False),
        sa.Column("max_severity", sa.String(length=16), nullable=False),
        sa.Column("ast_codes", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_assessments_severity", "skill_assessments", ["max_severity"])
    op.create_table(
        "skill_security_decisions",
        sa.Column("skill_id", sa.String(length=32), sa.ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("scope", sa.String(length=16), nullable=True),
        sa.Column("request_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("admin_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(length=32), nullable=True),
        sa.Column("decided_by", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_security_decisions_decision", "skill_security_decisions", ["decision"])


def downgrade() -> None:
    op.drop_index("ix_skill_security_decisions_decision", table_name="skill_security_decisions")
    op.drop_table("skill_security_decisions")
    op.drop_index("ix_skill_assessments_severity", table_name="skill_assessments")
    op.drop_table("skill_assessments")
