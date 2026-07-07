"""Data access: append-only message store, memory store, users, audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, bindparam, cast, func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from claw.db.models import (
    AppSetting,
    AuditEvent,
    ChatSession,
    Feedback,
    GuardrailRule,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDoc,
    LLMModel,
    LLMProvider,
    McpConnector,
    Memory,
    Message,
    Schedule,
    Share,
    Skill,
    UsageRecord,
    User,
    UserGroup,
)


class MessageStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def append(self, session_id: str, entries: list[dict[str, Any]]) -> None:
        """Append turn messages atomically with monotonic per-session seq."""
        if not entries:
            return
        async with self.factory() as db:
            next_seq = (
                await db.scalar(
                    select(func.coalesce(func.max(Message.seq), 0)).where(Message.session_id == session_id)
                )
            ) + 1
            for offset, entry in enumerate(entries):
                db.add(
                    Message(
                        session_id=session_id,
                        seq=next_seq + offset,
                        role=entry["role"],
                        content=entry.get("content") or "",
                        tool_calls=entry.get("tool_calls"),
                        tool_call_id=entry.get("tool_call_id"),
                        tool_name=entry.get("name"),
                        meta=entry.get("meta"),
                    )
                )
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def recent(
        self, session_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Load recent messages in chronological order, in LLM message format."""
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(Message)
                    .where(Message.session_id == session_id, Message.seq > after_seq)
                    .order_by(Message.seq.desc())
                    .limit(limit)
                )
            ).all()
        out: list[dict[str, Any]] = []
        for row in reversed(rows):
            entry: dict[str, Any] = {"role": row.role, "content": row.content}
            if row.tool_calls:
                entry["tool_calls"] = row.tool_calls
            if row.tool_call_id:
                entry["tool_call_id"] = row.tool_call_id
            if row.tool_name:
                entry["name"] = row.tool_name
            if row.meta:
                entry["meta"] = row.meta
            out.append(entry)
        return out

    async def max_seq(self, session_id: str) -> int:
        async with self.factory() as db:
            return await db.scalar(
                select(func.coalesce(func.max(Message.seq), 0)).where(Message.session_id == session_id)
            )

    async def total(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(Message))

    async def activity_by_day(self, days: int = 14) -> list[dict[str, Any]]:
        """User+assistant message counts per calendar day for the last `days` days.

        Returns a dense series (zero-filled) oldest→newest so the chart never has gaps.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days - 1)
        bucket = func.date_trunc("day", Message.created_at)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(bucket.label("day"), func.count())
                    .where(Message.created_at >= since, Message.role.in_(("user", "assistant")))
                    .group_by(bucket)
                )
            ).all()
        counts = {r[0].date().isoformat(): r[1] for r in rows if r[0] is not None}
        today = datetime.now(timezone.utc).date()
        series = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            series.append({"label": d, "count": counts.get(d, 0)})
        return series

    async def activity_by_hour(self) -> list[dict[str, Any]]:
        """Message counts bucketed by hour-of-day (0–23), dense/zero-filled."""
        hour = func.extract("hour", Message.created_at)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(hour.label("hour"), func.count())
                    .where(Message.role.in_(("user", "assistant")))
                    .group_by(hour)
                )
            ).all()
        counts = {int(r[0]): r[1] for r in rows if r[0] is not None}
        return [{"label": f"{h:02d}", "count": counts.get(h, 0)} for h in range(24)]


class SessionStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def create(self, user_id: str, title: str = "New chat", channel: str = "web") -> ChatSession:
        async with self.factory() as db:
            session = ChatSession(user_id=user_id, title=title, channel=channel)
            db.add(session)
            await db.commit()
            return session

    async def get(self, session_id: str) -> ChatSession | None:
        async with self.factory() as db:
            return await db.get(ChatSession, session_id)

    async def list_for_user(self, user_id: str) -> list[ChatSession]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(ChatSession)
                .where(ChatSession.user_id == user_id)
                .order_by(ChatSession.updated_at.desc())
            )
            return list(rows)

    async def rename(self, session_id: str, title: str) -> None:
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.title = title[:255]
                await db.commit()

    async def delete(self, session_id: str) -> None:
        async with self.factory() as db:
            await db.execute(Message.__table__.delete().where(Message.session_id == session_id))
            session = await db.get(ChatSession, session_id)
            if session is not None:
                await db.delete(session)
            await db.commit()

    async def count_by_user(self) -> dict[str, int]:
        async with self.factory() as db:
            rows = await db.execute(
                select(ChatSession.user_id, func.count()).group_by(ChatSession.user_id)
            )
            return {uid: n for uid, n in rows.all()}

    async def total(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(ChatSession))

    async def active_user_count(self, days: int = 7) -> int:
        """Distinct users with chat activity in the last `days` days."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.factory() as db:
            return await db.scalar(
                select(func.count(func.distinct(ChatSession.user_id))).where(
                    ChatSession.updated_at >= since
                )
            ) or 0

    async def set_consolidated_seq(self, session_id: str, seq: int) -> None:
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.last_consolidated_seq = seq
                await db.commit()

    async def set_model(self, session_id: str, model: str | None) -> None:
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None and session.model != model:
                session.model = model
                await db.commit()

    async def by_user_since(self, days: int = 7, limit: int = 20) -> list[dict[str, Any]]:
        """Sessions created in the last `days` days, grouped by user — highest first.

        Feeds the admin overview's "sessions by user" breakdown.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        ChatSession.user_id,
                        User.email,
                        User.display_name,
                        func.count(),
                    )
                    .join(User, User.id == ChatSession.user_id)
                    .where(ChatSession.created_at >= since)
                    .group_by(ChatSession.user_id, User.email, User.display_name)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "user_id": user_id,
                "label": display_name or email,
                "sessions": count,
            }
            for user_id, email, display_name, count in rows
        ]

    async def by_day_since(self, days: int = 7) -> list[dict[str, Any]]:
        """Sessions created per calendar day for the last `days` days, zero-filled."""
        since = datetime.now(timezone.utc) - timedelta(days=days - 1)
        bucket = func.date_trunc("day", ChatSession.created_at)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(bucket.label("day"), func.count())
                    .where(ChatSession.created_at >= since)
                    .group_by(bucket)
                )
            ).all()
        counts = {r[0].date().isoformat(): r[1] for r in rows if r[0] is not None}
        today = datetime.now(timezone.utc).date()
        series = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            series.append({"label": d, "count": counts.get(d, 0)})
        return series


class MemoryStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get_core(self, user_id: str) -> str:
        async with self.factory() as db:
            row = await db.scalar(
                select(Memory).where(Memory.user_id == user_id, Memory.kind == "core")
            )
            return row.content if row else ""

    async def set_core(self, user_id: str, content: str) -> None:
        async with self.factory() as db:
            row = await db.scalar(
                select(Memory).where(Memory.user_id == user_id, Memory.kind == "core")
            )
            if row is None:
                db.add(Memory(user_id=user_id, kind="core", content=content))
            else:
                row.content = content
            await db.commit()

    async def append_history(self, user_id: str, entry: str) -> None:
        if not entry.strip():
            return
        async with self.factory() as db:
            db.add(Memory(user_id=user_id, kind="history", content=entry.strip()))
            await db.commit()

    async def recent_history(self, user_id: str, limit: int = 20) -> list[str]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.kind == "history")
                .order_by(Memory.created_at.desc())
                .limit(limit)
            )
            return [row.content for row in reversed(list(rows))]

    async def stats(self) -> dict[str, int]:
        """Fleet-wide learning metrics for the admin overview: how many
        consolidation passes have run (each appends one history entry) and how
        many users have accumulated core memory."""
        async with self.factory() as db:
            consolidations = await db.scalar(
                select(func.count()).select_from(Memory).where(Memory.kind == "history")
            )
            memory_users = await db.scalar(
                select(func.count())
                .select_from(Memory)
                .where(Memory.kind == "core", func.length(Memory.content) > 0)
            )
        return {"consolidations": consolidations or 0, "memory_users": memory_users or 0}


class SkillStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def list_for_user(self, user_id: str) -> list[Skill]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Skill).where(Skill.user_id == user_id).order_by(Skill.name)
            )
            return list(rows)

    async def enabled_for_user(self, user_id: str) -> list[Skill]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Skill)
                .where(Skill.user_id == user_id, Skill.enabled.is_(True))
                .order_by(Skill.name)
            )
            return list(rows)

    async def get_by_name(self, user_id: str, name: str) -> Skill | None:
        async with self.factory() as db:
            return await db.scalar(
                select(Skill).where(Skill.user_id == user_id, Skill.name == name)
            )

    async def upsert(self, user_id: str, name: str, **fields: Any) -> Skill:
        async with self.factory() as db:
            skill = await db.scalar(
                select(Skill).where(Skill.user_id == user_id, Skill.name == name)
            )
            if skill is None:
                skill = Skill(user_id=user_id, name=name)
                db.add(skill)
            for key in ("description", "content", "enabled"):
                if key in fields and fields[key] is not None:
                    setattr(skill, key, fields[key])
            await db.commit()
            return skill

    async def delete(self, user_id: str, skill_id: str) -> bool:
        async with self.factory() as db:
            skill = await db.get(Skill, skill_id)
            if skill is None or skill.user_id != user_id:
                return False
            await db.delete(skill)
            await db.commit()
            return True


class ConnectorStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        # When set, connector env values are encrypted at rest and decrypted on read.
        self.secret_box = secret_box

    def _decrypt(self, row: McpConnector) -> McpConnector:
        if self.secret_box is not None and row.env:
            row.env = self.secret_box.decrypt_map(row.env)
        return row

    async def list_for_user(self, user_id: str) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector).where(McpConnector.user_id == user_id).order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def enabled_for_user(self, user_id: str) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector)
                .where(McpConnector.user_id == user_id, McpConnector.enabled.is_(True))
                .order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def upsert(self, user_id: str, name: str, **fields: Any) -> McpConnector:
        async with self.factory() as db:
            row = await db.scalar(
                select(McpConnector).where(McpConnector.user_id == user_id, McpConnector.name == name)
            )
            if row is None:
                row = McpConnector(user_id=user_id, name=name)
                db.add(row)
            for key in ("transport", "command", "url", "env", "enabled"):
                if key in fields and fields[key] is not None:
                    value = fields[key]
                    if key == "env" and self.secret_box is not None:
                        value = self.secret_box.encrypt_map(value)
                    setattr(row, key, value)
            await db.commit()
            self._decrypt(row)  # return plaintext env to the caller
            return row

    async def delete(self, user_id: str, connector_id: str) -> bool:
        async with self.factory() as db:
            row = await db.get(McpConnector, connector_id)
            if row is None or row.user_id != user_id:
                return False
            await db.delete(row)
            await db.commit()
            return True


class ScheduleStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def list_for_user(self, user_id: str) -> list[Schedule]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Schedule).where(Schedule.user_id == user_id).order_by(Schedule.created_at)
            )
            return list(rows)

    async def get(self, schedule_id: str) -> Schedule | None:
        async with self.factory() as db:
            return await db.get(Schedule, schedule_id)

    async def create(self, user_id: str, **fields: Any) -> Schedule:
        async with self.factory() as db:
            row = Schedule(user_id=user_id, **fields)
            db.add(row)
            await db.commit()
            return row

    async def update(self, user_id: str, schedule_id: str, **fields: Any) -> Schedule | None:
        async with self.factory() as db:
            row = await db.get(Schedule, schedule_id)
            if row is None or row.user_id != user_id:
                return None
            for key, value in fields.items():
                if value is not None and hasattr(row, key):
                    setattr(row, key, value)
            await db.commit()
            return row

    async def delete(self, user_id: str, schedule_id: str) -> bool:
        async with self.factory() as db:
            row = await db.get(Schedule, schedule_id)
            if row is None or row.user_id != user_id:
                return False
            await db.delete(row)
            await db.commit()
            return True

    async def due(self, now: datetime) -> list[Schedule]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Schedule).where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at <= now,
                )
            )
            return list(rows)

    async def mark_ran(
        self, schedule_id: str, *, next_run_at: datetime | None, status: str
    ) -> None:
        async with self.factory() as db:
            row = await db.get(Schedule, schedule_id)
            if row is None:
                return
            row.last_run_at = datetime.now(timezone.utc)
            row.last_status = status[:300]
            row.next_run_at = next_run_at
            if next_run_at is None and row.interval_seconds == 0 and not row.cron:
                row.enabled = False  # one-shot completed
            await db.commit()


class UserStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get_or_create_by_email(self, email: str, display_name: str = "") -> User:
        async with self.factory() as db:
            user = await db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, display_name=display_name or email.split("@")[0])
                db.add(user)
                await db.commit()
            return user

    async def get_by_email(self, email: str) -> User | None:
        async with self.factory() as db:
            return await db.scalar(select(User).where(User.email == email))

    async def count(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(User))

    async def create(
        self,
        email: str,
        *,
        password_hash: str = "",
        display_name: str = "",
        is_admin: bool = False,
        role: str = "user",
        group_id: str | None = None,
    ) -> User:
        async with self.factory() as db:
            user = User(
                email=email,
                display_name=display_name or email.split("@")[0],
                password_hash=password_hash,
                is_admin=is_admin,
                role=role,
                group_id=group_id,
            )
            db.add(user)
            await db.commit()
            return user

    async def assign_group(self, user_id: str, group_id: str | None) -> User | None:
        """Set (or clear, with None) a user's organizational group."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            user.group_id = group_id
            await db.commit()
            return user

    async def get(self, user_id: str) -> User | None:
        async with self.factory() as db:
            return await db.get(User, user_id)

    async def list_all(self) -> list[User]:
        async with self.factory() as db:
            rows = await db.scalars(select(User).order_by(User.created_at))
            return list(rows)

    async def count_admins(self) -> int:
        async with self.factory() as db:
            return await db.scalar(
                select(func.count()).select_from(User).where(User.is_admin.is_(True))
            )

    async def delete(self, user_id: str) -> bool:
        """Hard-delete a user and everything they own. Audit events are kept
        (their user_id has no FK) so the security trail survives the deletion."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return False
            # Messages hang off the user's sessions, so clear them first, then
            # the sessions and the rest of the per-user rows.
            session_ids = (
                await db.scalars(select(ChatSession.id).where(ChatSession.user_id == user_id))
            ).all()
            if session_ids:
                await db.execute(
                    Message.__table__.delete().where(Message.session_id.in_(session_ids))
                )
            # Private (BYOK) providers and their models are owned via owner_id.
            provider_ids = (
                await db.scalars(select(LLMProvider.id).where(LLMProvider.owner_id == user_id))
            ).all()
            if provider_ids:
                await db.execute(
                    LLMModel.__table__.delete().where(LLMModel.provider_id.in_(provider_ids))
                )
                await db.execute(
                    LLMProvider.__table__.delete().where(LLMProvider.owner_id == user_id)
                )
            for model in (ChatSession, Memory, Skill, McpConnector, Schedule, UsageRecord, Feedback):
                await db.execute(model.__table__.delete().where(model.user_id == user_id))
            await db.delete(user)
            await db.commit()
            return True

    async def update_flags(
        self,
        user_id: str,
        *,
        is_admin: bool | None = None,
        is_active: bool | None = None,
        role: str | None = None,
    ) -> User | None:
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            if is_admin is not None:
                user.is_admin = is_admin
                user.role = "admin" if is_admin else "user"
            if is_active is not None:
                user.is_active = is_active
            if role is not None:
                user.role = role
            await db.commit()
            return user

    async def update_profile(
        self, user_id: str, *, display_name: str | None = None, password_hash: str | None = None
    ) -> User | None:
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            if display_name is not None:
                user.display_name = display_name
            if password_hash:
                user.password_hash = password_hash
            await db.commit()
            return user

    async def set_role(self, user_id: str, role: str) -> None:
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is not None:
                user.role = role
                await db.commit()

    async def get_by_telegram_id(self, telegram_user_id: str) -> User | None:
        async with self.factory() as db:
            return await db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))

    async def set_telegram_id(self, user_id: str, telegram_user_id: str | None) -> None:
        async with self.factory() as db:
            # Clear any previous owner of this Telegram id (defensive; column is unique).
            if telegram_user_id is not None:
                prior = await db.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id)
                )
                if prior is not None and prior.id != user_id:
                    prior.telegram_user_id = None
            user = await db.get(User, user_id)
            if user is not None:
                user.telegram_user_id = telegram_user_id
            await db.commit()

    async def set_heartbeat(self, user_id: str, interval_seconds: int, next_at) -> None:
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is not None:
                user.heartbeat_interval_seconds = interval_seconds
                user.heartbeat_next_at = next_at
                await db.commit()

    async def heartbeat_due(self, now) -> list[User]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(User).where(
                    User.heartbeat_interval_seconds > 0,
                    User.heartbeat_next_at.is_not(None),
                    User.heartbeat_next_at <= now,
                )
            )
            return list(rows)


class GroupStore:
    """User groups — organization/filtering only, no policy or permission meaning."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def list(self) -> list[UserGroup]:
        async with self.factory() as db:
            rows = await db.scalars(select(UserGroup).order_by(UserGroup.name))
            return list(rows)

    async def get_by_name(self, name: str) -> UserGroup | None:
        async with self.factory() as db:
            return await db.scalar(select(UserGroup).where(UserGroup.name == name))

    async def create(self, name: str) -> UserGroup:
        async with self.factory() as db:
            row = UserGroup(name=name)
            db.add(row)
            await db.commit()
            return row

    async def delete(self, group_id: str) -> bool:
        """Remove a group; members are kept but become ungrouped (group_id → NULL).
        Done explicitly so it works even where the FK's ON DELETE isn't enforced
        (e.g. SQLite in tests)."""
        async with self.factory() as db:
            row = await db.get(UserGroup, group_id)
            if row is None:
                return False
            await db.execute(
                User.__table__.update().where(User.group_id == group_id).values(group_id=None)
            )
            await db.delete(row)
            await db.commit()
            return True

    async def set_default(self, group_id: str | None) -> None:
        """Make `group_id` the sole registration-default group, or clear the
        default entirely when None. Exactly one default at a time."""
        async with self.factory() as db:
            await db.execute(UserGroup.__table__.update().values(is_default=False))
            if group_id is not None:
                row = await db.get(UserGroup, group_id)
                if row is not None:
                    row.is_default = True
            await db.commit()

    async def default_group(self) -> UserGroup | None:
        async with self.factory() as db:
            return await db.scalar(select(UserGroup).where(UserGroup.is_default.is_(True)).limit(1))

    async def counts_by_group(self) -> dict[str, int]:
        """user_id count per group_id (excludes ungrouped)."""
        async with self.factory() as db:
            rows = await db.execute(
                select(User.group_id, func.count())
                .where(User.group_id.is_not(None))
                .group_by(User.group_id)
            )
            return {gid: n for gid, n in rows.all()}


class UsageStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def record(
        self, user_id: str, session_id: str | None, model: str, usage: dict[str, int]
    ) -> None:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        if prompt == 0 and completion == 0:
            return
        async with self.factory() as db:
            db.add(
                UsageRecord(
                    user_id=user_id,
                    session_id=session_id,
                    model=model,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )
            await db.commit()

    async def totals(self) -> dict[str, int]:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                        func.count(),
                    )
                )
            ).one()
        return {"prompt_tokens": row[0], "completion_tokens": row[1], "turns": row[2]}

    async def totals_for_user(self, user_id: str) -> dict[str, int]:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                        func.count(),
                    ).where(UsageRecord.user_id == user_id)
                )
            ).one()
        return {"prompt_tokens": row[0], "completion_tokens": row[1], "turns": row[2]}

    async def by_model(self, limit: int = 20) -> list[dict[str, Any]]:
        """Token usage grouped by model, highest total tokens first — feeds the
        admin overview's "tokens per model" breakdown."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        UsageRecord.model,
                        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                        func.count(),
                    )
                    .group_by(UsageRecord.model)
                    .order_by(
                        (
                            func.coalesce(func.sum(UsageRecord.prompt_tokens), 0)
                            + func.coalesce(func.sum(UsageRecord.completion_tokens), 0)
                        ).desc()
                    )
                    .limit(limit)
                )
            ).all()
        return [
            {
                "model": model or "(unknown)",
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "turns": turns,
            }
            for model, prompt, completion, turns in rows
        ]


class FeedbackStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def record(
        self, user_id: str, session_id: str | None, signal: str, note: str, message_preview: str
    ) -> None:
        async with self.factory() as db:
            db.add(
                Feedback(
                    user_id=user_id,
                    session_id=session_id,
                    signal=signal,
                    note=note[:2000],
                    message_preview=message_preview[:500],
                )
            )
            await db.commit()

    @staticmethod
    def _counts_query(user_id: str | None):
        q = select(Feedback.signal, func.count()).group_by(Feedback.signal)
        return q.where(Feedback.user_id == user_id) if user_id else q

    async def _counts(self, user_id: str | None) -> dict[str, int]:
        async with self.factory() as db:
            rows = (await db.execute(self._counts_query(user_id))).all()
        by = {sig: n for sig, n in rows}
        return {"up": by.get("up", 0), "down": by.get("down", 0)}

    async def totals(self) -> dict[str, int]:
        return await self._counts(None)

    async def totals_for_user(self, user_id: str) -> dict[str, int]:
        return await self._counts(user_id)


class AuditStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def log(
        self,
        kind: str,
        payload: dict[str, Any],
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        async with self.factory() as db:
            db.add(AuditEvent(kind=kind, payload=payload, user_id=user_id, session_id=session_id))
            await db.commit()

    async def list(
        self,
        kind: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
        before: datetime | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first audit rows with optional kind/user/text filters and a time cursor.

        `search` is a case-insensitive substring match over the JSON payload (and
        the event kind), so admins can find e.g. a tool name or a blocked rule.
        `before` (a created_at cursor) drives "load more" pagination.
        """
        stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(limit, 500))
        if kind:
            stmt = stmt.where(AuditEvent.kind == kind)
        if user_id:
            stmt = stmt.where(AuditEvent.user_id == user_id)
        if before is not None:
            stmt = stmt.where(AuditEvent.created_at < before)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                func.cast(AuditEvent.payload, String).ilike(like) | AuditEvent.kind.ilike(like)
            )
        async with self.factory() as db:
            rows = await db.scalars(stmt)
            return [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "payload": r.payload,
                    "user_id": r.user_id,
                    "session_id": r.session_id,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]

    async def kinds(self) -> list[str]:
        """Distinct event kinds present, for the filter UI."""
        async with self.factory() as db:
            rows = await db.execute(select(AuditEvent.kind).distinct())
            return sorted(k for (k,) in rows.all() if k)

    async def policy_hits_by_day(self, days: int = 14) -> list[dict[str, Any]]:
        """Guardrail-match counts (kind="policy" audit events, any scope) per
        calendar day for the last `days` days — dense/zero-filled like
        MessageStore.activity_by_day, for the admin overview chart."""
        since = datetime.now(timezone.utc) - timedelta(days=days - 1)
        bucket = func.date_trunc("day", AuditEvent.created_at)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(bucket.label("day"), func.count())
                    .where(AuditEvent.kind == "policy", AuditEvent.created_at >= since)
                    .group_by(bucket)
                )
            ).all()
        counts = {r[0].date().isoformat(): r[1] for r in rows if r[0] is not None}
        today = datetime.now(timezone.utc).date()
        return [
            {"label": (d := (today - timedelta(days=i)).isoformat()), "count": counts.get(d, 0)}
            for i in range(days - 1, -1, -1)
        ]


class LLMConfigStore:
    """Admin-managed LLM providers + models. Provider API keys are encrypted at rest."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        self.secret_box = secret_box

    def _enc(self, value: str) -> str:
        return self.secret_box.encrypt(value) if (self.secret_box and value) else value

    def _dec(self, value: str) -> str:
        return self.secret_box.decrypt(value) if (self.secret_box and value) else value

    @staticmethod
    def _clean_key(value: str) -> str:
        """Strip everything that isn't a printable ASCII, non-space character.

        API keys are ASCII; pasting one from a web page or chat easily smuggles
        in a non-breaking space (\\xa0), stray whitespace, or other unicode.
        Those aren't encodable in an HTTP Authorization header, so LiteLLM/httpx
        raise a cryptic `'ascii' codec can't encode` error surfaced to the user
        as a generic "internal error." Sanitizing here turns that into (at worst)
        a clear upstream auth error instead of a crash."""
        return "".join(ch for ch in (value or "") if 33 <= ord(ch) <= 126)

    # -- providers ----------------------------------------------------------
    # Ownership scope: owner_id=None operates on admin-global providers (owner_id
    # IS NULL); a non-null owner_id operates on that user's own private (BYOK)
    # providers. The same methods serve both — the Control Plane passes None, the
    # per-user "My Models" API passes the caller's id — so there is a single
    # implementation to maintain.
    async def list_providers(self, owner_id: str | None = None) -> list[LLMProvider]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(LLMProvider)
                .where(LLMProvider.owner_id == owner_id)
                .order_by(LLMProvider.name)
            )
            return list(rows)

    async def get_by_name(self, name: str, owner_id: str | None = None) -> LLMProvider | None:
        async with self.factory() as db:
            return await db.scalar(
                select(LLMProvider).where(
                    LLMProvider.name == name, LLMProvider.owner_id == owner_id
                )
            )

    async def create_provider(
        self,
        name: str,
        api_key: str,
        api_base: str,
        enabled: bool = True,
        model_prefix: str = "",
        owner_id: str | None = None,
    ) -> LLMProvider:
        async with self.factory() as db:
            row = LLMProvider(
                name=name,
                api_key=self._enc(self._clean_key(api_key)),
                api_base=api_base,
                enabled=enabled,
                model_prefix=model_prefix,
                owner_id=owner_id,
            )
            db.add(row)
            await db.commit()
            return row

    async def update_provider(
        self, provider_id: str, owner_id: str | None = None, **fields: Any
    ) -> LLMProvider | None:
        async with self.factory() as db:
            row = await db.get(LLMProvider, provider_id)
            # Ownership guard: a caller can only touch rows in its own scope, so a
            # user can never edit another user's (or a global) provider.
            if row is None or row.owner_id != owner_id:
                return None
            if "name" in fields and fields["name"]:
                row.name = fields["name"]
            if "api_base" in fields and fields["api_base"] is not None:
                row.api_base = fields["api_base"]
            if "enabled" in fields and fields["enabled"] is not None:
                row.enabled = fields["enabled"]
            if "model_prefix" in fields and fields["model_prefix"] is not None:
                row.model_prefix = fields["model_prefix"]
            # Only overwrite the key when a non-empty value is supplied.
            if fields.get("api_key"):
                row.api_key = self._enc(self._clean_key(fields["api_key"]))
            await db.commit()
            return row

    async def delete_provider(self, provider_id: str, owner_id: str | None = None) -> bool:
        async with self.factory() as db:
            row = await db.get(LLMProvider, provider_id)
            if row is None or row.owner_id != owner_id:
                return False
            await db.execute(LLMModel.__table__.delete().where(LLMModel.provider_id == provider_id))
            await db.delete(row)
            await db.commit()
            return True

    # -- models -------------------------------------------------------------
    async def list_models(self, owner_id: str | None = None) -> list[LLMModel]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(LLMModel)
                .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                .where(LLMProvider.owner_id == owner_id)
                .order_by(LLMModel.label)
            )
            return list(rows)

    async def _owns_provider(self, db: AsyncSession, provider_id: str, owner_id: str | None) -> bool:
        row = await db.get(LLMProvider, provider_id)
        return row is not None and row.owner_id == owner_id

    async def create_model(
        self,
        provider_id: str,
        model_id: str,
        label: str,
        enabled: bool = True,
        cost: str = "medium",
        description: str = "",
        owner_id: str | None = None,
    ) -> LLMModel | None:
        async with self.factory() as db:
            # The model inherits its provider's scope; only add it if the caller
            # owns that provider (global for admin, own for a user).
            if not await self._owns_provider(db, provider_id, owner_id):
                return None
            row = LLMModel(
                provider_id=provider_id,
                model_id=model_id,
                label=label or model_id,
                enabled=enabled,
                cost=cost or "medium",
                description=description or "",
            )
            db.add(row)
            await db.commit()
            return row

    async def update_model(
        self, model_id_pk: str, owner_id: str | None = None, **fields: Any
    ) -> LLMModel | None:
        async with self.factory() as db:
            row = await db.get(LLMModel, model_id_pk)
            if row is None or not await self._owns_provider(db, row.provider_id, owner_id):
                return None
            for key in ("model_id", "label", "enabled", "cost", "description"):
                if key in fields and fields[key] is not None:
                    setattr(row, key, fields[key])
            # The auto-selected default is an admin-global concept only; private
            # models are never the global default (owner_id=None gates it).
            if owner_id is None and fields.get("is_default"):
                # Exactly one default across all global models.
                await db.execute(
                    LLMModel.__table__.update()
                    .where(
                        LLMModel.provider_id.in_(
                            select(LLMProvider.id).where(LLMProvider.owner_id.is_(None))
                        )
                    )
                    .values(is_default=False)
                )
                row.is_default = True
            elif owner_id is None and fields.get("is_default") is False:
                row.is_default = False
            await db.commit()
            return row

    async def delete_model(self, model_id_pk: str, owner_id: str | None = None) -> bool:
        async with self.factory() as db:
            row = await db.get(LLMModel, model_id_pk)
            if row is None or not await self._owns_provider(db, row.provider_id, owner_id):
                return False
            await db.delete(row)
            await db.commit()
            return True

    # -- resolution (used by chat/runtime) ----------------------------------
    async def enabled_models(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """Enabled models for the chat picker: admin-global models merged with the
        caller's own private (BYOK) models. Each carries a ``scope`` marker
        ("global" | "private") so the UI can badge the user's own."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        or_(
                            LLMProvider.owner_id.is_(None),
                            LLMProvider.owner_id == user_id,
                        ),
                    )
                    .order_by(LLMProvider.name, LLMModel.label)
                )
            ).all()
        return [
            {
                "model_id": m.model_id,
                "label": m.label or m.model_id,
                "provider": p.name,
                "is_default": m.is_default,
                "cost": m.cost or "medium",
                "description": m.description or "",
                "scope": "private" if p.owner_id else "global",
            }
            for m, p in rows
        ]

    async def default_model(self) -> str | None:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        LLMModel.is_default.is_(True),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row.model_id if row else None

    async def resolve(self, model_id: str, user_id: str | None = None) -> dict[str, str] | None:
        """Given an enabled model id, return its provider credentials (decrypted).

        Scope: admin-global providers plus the caller's own private ones. If the
        same model id exists in both scopes, the user's own wins (ordered first) —
        so a user's key is used for their model and one user can never resolve
        another user's credentials."""
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.model_id == model_id,
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        or_(
                            LLMProvider.owner_id.is_(None),
                            LLMProvider.owner_id == user_id,
                        ),
                    )
                    # Prefer the caller's own provider (owner_id NOT NULL) on a tie:
                    # is_(None) is False(0) for private rows, so ascending puts them first.
                    .order_by(LLMProvider.owner_id.is_(None))
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        m, p = row
        # Sanitize on read too, so keys stored before sanitization existed (or
        # any stray whitespace) can't crash the outbound HTTP header encoding.
        return {
            "model_id": m.model_id,
            "api_key": self._clean_key(self._dec(p.api_key)),
            "api_base": p.api_base,
        }


class GuardrailStore:
    """Persists control-policy rules + the monitor-only toggle."""

    _MONITOR_KEY = "guardrail_monitor_only"

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def list_rules(self) -> list[GuardrailRule]:
        async with self.factory() as db:
            rows = await db.scalars(select(GuardrailRule).order_by(GuardrailRule.created_at))
            return list(rows)

    async def count(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(GuardrailRule))

    async def seed(self, rules: list[dict[str, Any]]) -> None:
        """Insert built-in rules once, when the table is empty."""
        async with self.factory() as db:
            for r in rules:
                db.add(GuardrailRule(**r))
            await db.commit()

    async def create_rule(self, **fields: Any) -> GuardrailRule:
        async with self.factory() as db:
            row = GuardrailRule(**fields)
            db.add(row)
            await db.commit()
            return row

    async def update_rule(self, rule_id: str, **fields: Any) -> GuardrailRule | None:
        async with self.factory() as db:
            row = await db.get(GuardrailRule, rule_id)
            if row is None:
                return None
            for key in ("name", "pattern", "action", "scopes", "placeholder", "severity",
                        "block_message", "enabled"):
                if key in fields and fields[key] is not None:
                    setattr(row, key, fields[key])
            await db.commit()
            return row

    async def delete_rule(self, rule_id: str) -> bool:
        async with self.factory() as db:
            row = await db.get(GuardrailRule, rule_id)
            if row is None or row.is_builtin:
                return False  # built-ins can be disabled but not deleted
            await db.delete(row)
            await db.commit()
            return True

    async def get_monitor_only(self, default: bool = False) -> bool:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._MONITOR_KEY)
            if row is None:
                return default
            return bool(row.value.get("value", default))

    async def set_monitor_only(self, value: bool) -> None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._MONITOR_KEY)
            if row is None:
                db.add(AppSetting(key=self._MONITOR_KEY, value={"value": value}))
            else:
                row.value = {"value": value}
            await db.commit()


class OAuthAppStore:
    """Admin-registered Google/Microsoft OAuth app credentials, used by the
    one-click connector-connect flow. Client secrets are encrypted at rest."""

    _KEYS = {"google": "oauth_app_google", "microsoft": "oauth_app_microsoft"}

    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        self.secret_box = secret_box

    async def get(self, provider: str) -> dict[str, str]:
        """Full credentials (decrypted secret) — for the OAuth flow. {} if unset."""
        key = self._KEYS.get(provider)
        if key is None:
            return {}
        async with self.factory() as db:
            row = await db.get(AppSetting, key)
        if row is None:
            return {}
        data = dict(row.value or {})
        if self.secret_box and data.get("client_secret"):
            data["client_secret"] = self.secret_box.decrypt(data["client_secret"])
        return data

    async def public(self, provider: str) -> dict[str, Any]:
        """Safe view for the admin UI — never returns the secret itself."""
        data = await self.get(provider)
        return {
            "client_id": data.get("client_id", ""),
            "tenant": data.get("tenant", ""),
            "has_secret": bool(data.get("client_secret")),
        }

    async def set(self, provider: str, client_id: str, client_secret: str, tenant: str = "") -> None:
        key = self._KEYS.get(provider)
        if key is None:
            raise ValueError(f"unknown provider {provider}")
        # Preserve the existing secret when the caller submits an empty one.
        existing = await self.get(provider)
        secret = client_secret or existing.get("client_secret", "")
        stored_secret = self.secret_box.encrypt(secret) if (self.secret_box and secret) else secret
        value = {"client_id": client_id, "client_secret": stored_secret, "tenant": tenant}
        async with self.factory() as db:
            row = await db.get(AppSetting, key)
            if row is None:
                db.add(AppSetting(key=key, value=value))
            else:
                row.value = value
            await db.commit()


class TelegramConfigStore:
    """Admin-configured Telegram bot token, so the integration is turned on
    self-service from the Admin console instead of an env var + server restart.
    The token is encrypted at rest (same scheme as OAuthAppStore/LLMConfigStore).

    ``get()`` returns None when nobody has ever saved a config through the admin
    UI — callers should fall back to the CLAW_TELEGRAM_BOT_TOKEN env var in that
    case, so existing infra-managed deployments keep working unchanged. Once an
    admin saves anything here, this store is authoritative.
    """

    _KEY = "telegram_bot"

    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        self.secret_box = secret_box

    async def get(self) -> dict[str, Any] | None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
        if row is None:
            return None
        data = dict(row.value or {})
        token = data.get("bot_token", "")
        if self.secret_box and token:
            token = self.secret_box.decrypt(token)
        return {"bot_token": token, "enabled": bool(data.get("enabled", True))}

    async def public(self) -> dict[str, Any]:
        """Safe view for the admin UI — never returns the token itself."""
        data = await self.get()
        if data is None:
            return {"has_token": False, "enabled": False}
        return {"has_token": bool(data["bot_token"]), "enabled": data["enabled"]}

    async def set(self, bot_token: str, enabled: bool) -> None:
        # Preserve the existing token when the caller submits a blank one (the
        # admin UI does this when only toggling `enabled`, not the token).
        existing = await self.get()
        token = bot_token or (existing["bot_token"] if existing else "")
        stored_token = self.secret_box.encrypt(token) if (self.secret_box and token) else token
        value = {"bot_token": stored_token, "enabled": enabled}
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            if row is None:
                db.add(AppSetting(key=self._KEY, value=value))
            else:
                row.value = value
            await db.commit()


class KnowledgeStore:
    """Knowledge bases (OKF bundles) + their documents and searchable chunks.

    Retrieval uses pg_trgm word-similarity on Postgres (language-agnostic — good
    for Thai and English without an embedding model); a plain ILIKE fallback
    keeps it working on SQLite (tests).
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres

    # -- bases --------------------------------------------------------------
    async def create_base(
        self, owner_id: str, name: str, description: str = "", visibility: str = "private"
    ) -> KnowledgeBase:
        async with self.factory() as db:
            kb = KnowledgeBase(
                owner_id=owner_id,
                name=name[:120],
                description=description,
                visibility="public" if visibility == "public" else "private",
            )
            db.add(kb)
            await db.commit()
            await db.refresh(kb)
            return kb

    async def get_base(self, kb_id: str) -> KnowledgeBase | None:
        async with self.factory() as db:
            return await db.get(KnowledgeBase, kb_id)

    async def update_base(self, kb_id: str, **fields: Any) -> KnowledgeBase | None:
        async with self.factory() as db:
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is None:
                return None
            if fields.get("name"):
                kb.name = str(fields["name"])[:120]
            if fields.get("description") is not None:
                kb.description = str(fields["description"])
            if fields.get("visibility") in ("private", "public"):
                kb.visibility = fields["visibility"]
            await db.commit()
            await db.refresh(kb)
            return kb

    async def list_accessible(self, user_id: str) -> list[dict[str, Any]]:
        """Bases the user can see (their own + all public), each with a doc count."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(KnowledgeBase)
                    .where(
                        (KnowledgeBase.owner_id == user_id)
                        | (KnowledgeBase.visibility == "public")
                    )
                    .order_by(KnowledgeBase.updated_at.desc())
                )
            ).scalars().all()
            counts = dict(
                (
                    await db.execute(
                        select(KnowledgeDoc.kb_id, func.count()).group_by(KnowledgeDoc.kb_id)
                    )
                ).all()
            )
        return [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description,
                "visibility": kb.visibility,
                "owner_id": kb.owner_id,
                "is_owner": kb.owner_id == user_id,
                "docs": int(counts.get(kb.id, 0)),
                "updated_at": kb.updated_at.isoformat(),
            }
            for kb in rows
        ]

    async def accessible_ids(self, user_id: str) -> list[str]:
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(KnowledgeBase.id).where(
                        (KnowledgeBase.owner_id == user_id)
                        | (KnowledgeBase.visibility == "public")
                    )
                )
            ).scalars().all()
        return list(rows)

    async def delete_base(self, kb_id: str) -> None:
        async with self.factory() as db:
            await db.execute(KnowledgeChunk.__table__.delete().where(KnowledgeChunk.kb_id == kb_id))
            await db.execute(KnowledgeDoc.__table__.delete().where(KnowledgeDoc.kb_id == kb_id))
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is not None:
                await db.delete(kb)
            await db.commit()

    # -- documents ----------------------------------------------------------
    async def add_doc(
        self,
        *,
        kb_id: str,
        concept_id: str,
        title: str,
        filename: str,
        mime: str,
        size: int,
        chars: int,
        chunk_texts: list[str],
    ) -> KnowledgeDoc:
        async with self.factory() as db:
            doc = KnowledgeDoc(
                kb_id=kb_id,
                concept_id=concept_id,
                title=title[:255],
                filename=filename[:255],
                mime=mime[:120],
                size=size,
                chars=chars,
                chunks=len(chunk_texts),
            )
            db.add(doc)
            await db.flush()
            for i, text in enumerate(chunk_texts):
                db.add(
                    KnowledgeChunk(kb_id=kb_id, doc_id=doc.id, seq=i, title=title[:255], text=text)
                )
            kb = await db.get(KnowledgeBase, kb_id)
            if kb is not None:
                kb.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(doc)
            return doc

    async def list_docs(self, kb_id: str) -> list[KnowledgeDoc]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(KnowledgeDoc)
                .where(KnowledgeDoc.kb_id == kb_id)
                .order_by(KnowledgeDoc.created_at.desc())
            )
            return list(rows)

    async def get_doc(self, doc_id: str) -> KnowledgeDoc | None:
        async with self.factory() as db:
            return await db.get(KnowledgeDoc, doc_id)

    async def delete_doc(self, doc_id: str) -> KnowledgeDoc | None:
        async with self.factory() as db:
            doc = await db.get(KnowledgeDoc, doc_id)
            if doc is None:
                return None
            await db.execute(KnowledgeChunk.__table__.delete().where(KnowledgeChunk.doc_id == doc_id))
            await db.delete(doc)
            await db.commit()
            return doc

    # -- retrieval ----------------------------------------------------------
    async def search(self, query: str, kb_ids: list[str], limit: int = 6) -> list[dict[str, Any]]:
        """Top matching chunks across the given bases, best first."""
        query = (query or "").strip()
        if not query or not kb_ids:
            return []
        like = f"%{query}%"
        async with self.factory() as db:
            if self.is_postgres:
                stmt = (
                    sa_text(
                        "SELECT c.text, c.title, c.kb_id, b.name AS kb_name, "
                        "word_similarity(:q, c.text) AS score "
                        "FROM knowledge_chunks c JOIN knowledge_bases b ON b.id = c.kb_id "
                        "WHERE c.kb_id IN :ids "
                        "AND (word_similarity(:q, c.text) > 0.12 OR c.text ILIKE :like) "
                        "ORDER BY score DESC LIMIT :limit"
                    )
                    .bindparams(bindparam("ids", expanding=True))
                )
                rows = (
                    await db.execute(
                        stmt, {"q": query, "ids": kb_ids, "like": like, "limit": limit}
                    )
                ).all()
                return [
                    {"text": r[0], "title": r[1], "kb_id": r[2], "kb_name": r[3], "score": float(r[4])}
                    for r in rows
                ]
            # SQLite fallback: simple substring match.
            rows = (
                await db.execute(
                    select(KnowledgeChunk.text, KnowledgeChunk.title, KnowledgeChunk.kb_id)
                    .where(KnowledgeChunk.kb_id.in_(kb_ids), KnowledgeChunk.text.ilike(like))
                    .limit(limit)
                )
            ).all()
            return [{"text": r[0], "title": r[1], "kb_id": r[2], "kb_name": "", "score": 1.0} for r in rows]


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareStore:
    """Public read-only share links. Stores only the SHA-256 of each token, so a
    DB leak can't reconstruct live links (the plaintext token lives only in the
    URL). Lookups hash the incoming token and match; expired/revoked shares are
    treated as gone."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def create(
        self,
        *,
        user_id: str,
        session_id: str | None,
        title: str,
        snapshot: dict[str, Any],
        ttl_days: int = 7,
    ) -> tuple[Share, str]:
        """Create a share; returns (row, plaintext_token). The token is shown to
        the user once (in the URL) and never stored in the clear."""
        import secrets

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        async with self.factory() as db:
            share = Share(
                token_hash=_hash_token(token),
                user_id=user_id,
                session_id=session_id,
                title=title[:255] or "Shared answer",
                snapshot=snapshot,
                expires_at=expires_at,
            )
            db.add(share)
            await db.commit()
            await db.refresh(share)
            return share, token

    async def get_active_by_token(self, token: str, *, bump: bool = True) -> Share | None:
        """Return a live (not revoked, not expired) share for a plaintext token,
        optionally bumping its view counter. Returns None otherwise."""
        if not token:
            return None
        async with self.factory() as db:
            share = (
                await db.scalars(
                    select(Share).where(Share.token_hash == _hash_token(token))
                )
            ).first()
            if share is None or share.revoked:
                return None
            if share.expires_at is not None and share.expires_at < datetime.now(timezone.utc):
                return None
            if bump:
                share.view_count = (share.view_count or 0) + 1
                await db.commit()
                await db.refresh(share)
            return share

    async def revoke(self, share_id: str, user_id: str) -> bool:
        """Revoke a share the user owns. Returns True if a row was updated."""
        async with self.factory() as db:
            share = await db.get(Share, share_id)
            if share is None or share.user_id != user_id:
                return False
            share.revoked = True
            await db.commit()
            return True
