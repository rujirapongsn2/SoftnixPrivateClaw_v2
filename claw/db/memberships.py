"""Group membership helpers shared by the claw and sbot stores.

`user_group_members` is the source of truth. `users.group_id` mirrors the user's primary
(first) group for older readers and for rollback; never use it to grant access.
"""

from collections.abc import Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from claw.db.models import User, UserGroup, UserGroupMember


def dedupe(group_ids: Iterable[str | None]) -> list[str]:
    """Order-preserving, drops blanks/None and repeats."""
    return list(dict.fromkeys(g for g in group_ids if g))


def member_group_ids_subquery(user_id):
    """SELECT group_id of every group `user_id` belongs to — for `col.in_(...)`/EXISTS filters."""
    return select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)


async def group_ids_of(db: AsyncSession, user_id: str) -> list[str]:
    rows = await db.execute(
        select(UserGroupMember.group_id)
        .where(UserGroupMember.user_id == user_id)
        .order_by(UserGroupMember.position, UserGroupMember.created_at, UserGroupMember.group_id)
    )
    return list(rows.scalars())


async def memberships_of(db: AsyncSession, user_ids: list[str]) -> dict[str, list[str]]:
    """{user_id: [group_id, ...]} for many users in one query (primary group first)."""
    out: dict[str, list[str]] = {uid: [] for uid in user_ids}
    if not user_ids:
        return out
    rows = await db.execute(
        select(UserGroupMember.user_id, UserGroupMember.group_id)
        .where(UserGroupMember.user_id.in_(user_ids))
        .order_by(UserGroupMember.position, UserGroupMember.created_at, UserGroupMember.group_id)
    )
    for uid, gid in rows.all():
        out[uid].append(gid)
    return out


async def set_memberships(db: AsyncSession, user: User, group_ids: Iterable[str | None]) -> list[str]:
    """Replace `user`'s groups with `group_ids` (the first becomes the primary/mirrored group).

    Does not commit. Unknown group ids are rejected so a bad id can't silently drop a member into
    nothing; the caller validates for friendlier errors."""
    wanted = dedupe(group_ids)
    if wanted:
        known = set((await db.execute(select(UserGroup.id).where(UserGroup.id.in_(wanted)))).scalars())
        missing = [g for g in wanted if g not in known]
        if missing:
            raise ValueError(f"unknown group: {missing[0]}")
    current = await group_ids_of(db, user.id)
    for gid in current:
        if gid not in wanted:
            await db.execute(
                delete(UserGroupMember).where(UserGroupMember.user_id == user.id, UserGroupMember.group_id == gid)
            )
    for position, gid in enumerate(wanted):
        if gid in current:
            await db.execute(
                update(UserGroupMember)
                .where(UserGroupMember.user_id == user.id, UserGroupMember.group_id == gid)
                .values(position=position)
            )
        else:
            db.add(UserGroupMember(user_id=user.id, group_id=gid, position=position))
    user.group_id = wanted[0] if wanted else None  # mirror of the primary group
    return wanted


async def counts_by_group(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(UserGroupMember.group_id, func.count()).group_by(UserGroupMember.group_id)
    )
    return {gid: n for gid, n in rows.all()}
