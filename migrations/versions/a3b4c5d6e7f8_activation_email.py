"""users.activation_email_sent_at + case-insensitive unique email index

Adds a nullable timestamp so the imported-user activation-email sender can
enforce a resend cooldown, and a unique index on lower(email) so two accounts
differing only by case can no longer both be created — closing the gap left
by the application-level case-insensitive lookups (UserStore.get_by_email())
added without a matching DB-level guarantee. No pre-existing duplicate-case
rows were found in production before this was added.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-12 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('activation_email_sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    bind = op.get_bind()

    # Guard the unique index against pre-existing case-only duplicates instead
    # of letting CREATE UNIQUE INDEX abort with an opaque constraint-violation
    # mid-upgrade — a self-hosted operator (unlike the one deployment checked
    # when this was written) may already have e.g. both Jane@x.com and
    # jane@x.com, since the old constraint was case-sensitive.
    dupes = bind.execute(
        sa.text(
            "SELECT lower(email) AS e, count(*) AS n FROM users GROUP BY lower(email) HAVING count(*) > 1"
        )
    ).fetchall()
    if dupes:
        sample = ", ".join(row[0] for row in dupes[:10])
        more = f" (+{len(dupes) - 10} more)" if len(dupes) > 10 else ""
        raise RuntimeError(
            "Cannot add a case-insensitive unique index on users.email: "
            f"{len(dupes)} email address(es) already exist under more than one "
            f"case variant (e.g. {sample}{more}). Merge, rename, or delete the "
            "duplicate account(s) for each of these emails before re-running "
            "this migration."
        )

    if bind.dialect.name == "postgresql":
        op.execute("CREATE UNIQUE INDEX ix_users_email_lower ON users (lower(email))")
    else:
        # SQLite (tests only) — op.create_index supports a raw text() expression.
        op.create_index(
            'ix_users_email_lower', 'users', [sa.text('lower(email)')], unique=True
        )


def downgrade() -> None:
    op.drop_index('ix_users_email_lower', table_name='users')
    op.drop_column('users', 'activation_email_sent_at')
