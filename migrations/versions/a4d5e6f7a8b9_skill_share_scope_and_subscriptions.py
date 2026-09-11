"""Pin Group skill sharing to a concrete group and add recipient opt-ins.

Revision ID: a4d5e6f7a8b9
Revises: a3d4e5f6b7c8
Create Date: 2026-09-11 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4d5e6f7a8b9"
down_revision: Union[str, None] = "a3d4e5f6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("shared_group_id", sa.String(length=32), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_skills_shared_group_id_user_groups",
            "skills",
            "user_groups",
            ["shared_group_id"],
            ["id"],
            ondelete="SET NULL",
        )
    # Existing Group shares targeted the owner's then-current group. Preserve
    # that scope during the upgrade so later owner transfers cannot retarget it.
    op.execute(
        """
        UPDATE skills
        SET shared_group_id = (SELECT group_id FROM users WHERE users.id = skills.user_id)
        WHERE visibility = 'group'
        """
    )
    op.create_index("ix_skills_shared_group_id", "skills", ["shared_group_id"])
    op.create_index("ix_skills_share_scope", "skills", ["visibility", "enabled", "shared_group_id"])
    op.create_table(
        "skill_subscriptions",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("skill_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "skill_id"),
    )
    op.create_index("ix_skill_subscriptions_skill", "skill_subscriptions", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_subscriptions_skill", table_name="skill_subscriptions")
    op.drop_table("skill_subscriptions")
    op.drop_index("ix_skills_share_scope", table_name="skills")
    op.drop_index("ix_skills_shared_group_id", table_name="skills")
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_skills_shared_group_id_user_groups", "skills", type_="foreignkey")
    op.drop_column("skills", "shared_group_id")
