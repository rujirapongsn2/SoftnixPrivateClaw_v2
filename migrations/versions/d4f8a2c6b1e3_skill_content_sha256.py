"""Skill content digest

Adds `skills.content_sha256`, the digest a skill review is stored under
(claw/skills/assessment.py::content_digest). A review counts only while its sha
matches this column, so a skill whose text changed never keeps the old text's
verdict, whichever code path changed it.

Existing rows are filled here in batches. Downgrade drops the column.

Revision ID: d4f8a2c6b1e3
Revises: c3e7b1d9a4f2
Create Date: 2026-11-04 18:00:00.000000
"""

import hashlib
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f8a2c6b1e3"
down_revision: Union[str, None] = "c3e7b1d9a4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _digest(content: str, bundle_id: str | None, description: str) -> str:
    # Frozen copy of content_digest: a migration must not change when the app code does.
    content, description = content.replace("\0", ""), description.replace("\0", "")
    h = hashlib.sha256()
    h.update(b"SKILL.md\0")
    h.update(content.encode("utf-8"))
    h.update(b"\0description\0" + description.encode("utf-8"))
    h.update(b"\0bundle\0" + (bundle_id or "").encode("utf-8"))
    return h.hexdigest()


def upgrade() -> None:
    op.add_column("skills", sa.Column("content_sha256", sa.String(length=64), nullable=False, server_default=""))
    skills = sa.table(
        "skills", sa.column("id"), sa.column("content"), sa.column("description"), sa.column("bundle_id"),
        sa.column("content_sha256"),
    )
    bind, after = op.get_bind(), ""
    while rows := bind.execute(
        sa.select(skills.c.id, skills.c.content, skills.c.description, skills.c.bundle_id)
        .where(skills.c.id > after).order_by(skills.c.id).limit(200)
    ).all():
        bind.execute(  # one executemany per batch, not a round trip per skill
            skills.update().where(skills.c.id == sa.bindparam("_id")).values(content_sha256=sa.bindparam("_sha")),
            [{"_id": r.id, "_sha": _digest(r.content or "", r.bundle_id, r.description or "")} for r in rows],
        )
        after = rows[-1].id


def downgrade() -> None:
    op.drop_column("skills", "content_sha256")
