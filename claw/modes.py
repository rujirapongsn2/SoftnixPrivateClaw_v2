"""Shared identity and mode-specific adapters."""

from types import SimpleNamespace
from sqlalchemy import select, delete
from claw.db.models import User
from sbot.db.stores import UserStore


class SharedUserStore(UserStore):
    async def delete(self, user_id):
        # Delete both mode domains and shared dependent data in FK order in one transaction.
        from claw.db.models import Base

        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return False
            conditions = {"users": User.__table__.c.id == user_id}
            for table in Base.metadata.sorted_tables:
                if table.name == "users":
                    continue
                own = next((table.c[k] == user_id for k in ("owner_id", "user_id") if k in table.c), None)
                if own is not None:
                    conditions[table.name] = own
                else:
                    for fk in table.foreign_keys:
                        parent = fk.column.table
                        if parent.name in conditions:
                            conditions[table.name] = fk.parent.in_(
                                select(fk.column).where(conditions[parent.name])
                            )
                            break
            for table in reversed(Base.metadata.sorted_tables):
                if table.name in conditions and table.name not in {"users", "audit_events"}:
                    await db.execute(delete(table).where(conditions[table.name]))
            await db.delete(user)
            await db.commit()
            return True


class SbotHeartbeatUsers:
    def __init__(self, users):
        self.users = users

    async def set_heartbeat(self, user_id, interval_seconds, next_at):
        async with self.users.factory() as db:
            user = await db.get(User, user_id)
            if user:
                user.sbot_heartbeat_interval_seconds = interval_seconds
                user.sbot_heartbeat_next_at = next_at
                await db.commit()

    async def heartbeat_due(self, now):
        async with self.users.factory() as db:
            rows = await db.scalars(
                select(User).where(
                    User.is_active.is_(True),
                    User.sbot_heartbeat_interval_seconds > 0,
                    User.sbot_heartbeat_next_at <= now,
                )
            )
            return [
                SimpleNamespace(id=u.id, heartbeat_interval_seconds=u.sbot_heartbeat_interval_seconds)
                for u in rows
            ]


class CombinedReports:
    """Read-only admin aggregation; runtime data stores remain mode-local."""

    def __init__(self, left, right):
        self.left, self.right = left, right
        self.factory = left.factory

    def __getattr__(self, name):
        if name not in {
            "count_by_user",
            "total",
            "stats",
            "totals",
            "activity_by_day",
            "activity_by_hour",
            "activity_by_day_hour",
            "by_day_since",
            "by_user_since",
        }:
            raise AttributeError(name)

        async def combined(*args, **kwargs):
            import asyncio

            a, b = await asyncio.gather(
                getattr(self.left, name)(*args, **kwargs), getattr(self.right, name)(*args, **kwargs)
            )
            if isinstance(a, int):
                return a + b
            if isinstance(a, dict):
                return {k: a.get(k, 0) + b.get(k, 0) for k in a.keys() | b.keys()}
            key = "user_id" if name == "by_user_since" else "label"
            metric = "sessions" if name == "by_user_since" else "count"
            result = {row[key]: dict(row) for row in a}
            for row in b:
                if row[key] in result:
                    result[row[key]][metric] += row[metric]
                else:
                    result[row[key]] = dict(row)
            return (
                sorted(result.values(), key=lambda row: -row[metric])
                if name == "by_user_since"
                else list(result.values())
            )

        return combined

    async def active_user_count(self, days=7):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import union, func
        from claw.db.models import ChatSession
        from sbot.db.models import ChatSession as SbotSession

        since = datetime.now(timezone.utc) - timedelta(days=days)
        ids = union(
            select(ChatSession.user_id).where(ChatSession.updated_at >= since),
            select(SbotSession.user_id).where(SbotSession.updated_at >= since),
        ).subquery()
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(ids))
