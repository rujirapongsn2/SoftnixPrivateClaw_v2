"""Data access: append-only message store, memory store, users, audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from claw.db.models import (
    AppSetting,
    AuditEvent,
    ChatSession,
    Feedback,
    GuardrailRule,
    LLMModel,
    LLMProvider,
    McpConnector,
    Memory,
    Message,
    Schedule,
    Skill,
    UsageRecord,
    User,
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
    ) -> User:
        async with self.factory() as db:
            user = User(
                email=email,
                display_name=display_name or email.split("@")[0],
                password_hash=password_hash,
                is_admin=is_admin,
                role=role,
            )
            db.add(user)
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


class LLMConfigStore:
    """Admin-managed LLM providers + models. Provider API keys are encrypted at rest."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        self.secret_box = secret_box

    def _enc(self, value: str) -> str:
        return self.secret_box.encrypt(value) if (self.secret_box and value) else value

    def _dec(self, value: str) -> str:
        return self.secret_box.decrypt(value) if (self.secret_box and value) else value

    # -- providers ----------------------------------------------------------
    async def list_providers(self) -> list[LLMProvider]:
        async with self.factory() as db:
            rows = await db.scalars(select(LLMProvider).order_by(LLMProvider.name))
            return list(rows)

    async def get_by_name(self, name: str) -> LLMProvider | None:
        async with self.factory() as db:
            return await db.scalar(select(LLMProvider).where(LLMProvider.name == name))

    async def create_provider(
        self, name: str, api_key: str, api_base: str, enabled: bool = True
    ) -> LLMProvider:
        async with self.factory() as db:
            row = LLMProvider(
                name=name, api_key=self._enc(api_key), api_base=api_base, enabled=enabled
            )
            db.add(row)
            await db.commit()
            return row

    async def update_provider(self, provider_id: str, **fields: Any) -> LLMProvider | None:
        async with self.factory() as db:
            row = await db.get(LLMProvider, provider_id)
            if row is None:
                return None
            if "name" in fields and fields["name"]:
                row.name = fields["name"]
            if "api_base" in fields and fields["api_base"] is not None:
                row.api_base = fields["api_base"]
            if "enabled" in fields and fields["enabled"] is not None:
                row.enabled = fields["enabled"]
            # Only overwrite the key when a non-empty value is supplied.
            if fields.get("api_key"):
                row.api_key = self._enc(fields["api_key"])
            await db.commit()
            return row

    async def delete_provider(self, provider_id: str) -> bool:
        async with self.factory() as db:
            await db.execute(LLMModel.__table__.delete().where(LLMModel.provider_id == provider_id))
            row = await db.get(LLMProvider, provider_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    # -- models -------------------------------------------------------------
    async def list_models(self) -> list[LLMModel]:
        async with self.factory() as db:
            rows = await db.scalars(select(LLMModel).order_by(LLMModel.label))
            return list(rows)

    async def create_model(
        self,
        provider_id: str,
        model_id: str,
        label: str,
        enabled: bool = True,
        cost: str = "medium",
        description: str = "",
    ) -> LLMModel:
        async with self.factory() as db:
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

    async def update_model(self, model_id_pk: str, **fields: Any) -> LLMModel | None:
        async with self.factory() as db:
            row = await db.get(LLMModel, model_id_pk)
            if row is None:
                return None
            for key in ("model_id", "label", "enabled", "cost", "description"):
                if key in fields and fields[key] is not None:
                    setattr(row, key, fields[key])
            if fields.get("is_default"):
                # Exactly one default across all models.
                await db.execute(LLMModel.__table__.update().values(is_default=False))
                row.is_default = True
            elif fields.get("is_default") is False:
                row.is_default = False
            await db.commit()
            return row

    async def delete_model(self, model_id_pk: str) -> bool:
        async with self.factory() as db:
            row = await db.get(LLMModel, model_id_pk)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
            return True

    # -- resolution (used by chat/runtime) ----------------------------------
    async def enabled_models(self) -> list[dict[str, Any]]:
        """Enabled models joined with their (enabled) provider, for the chat picker."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(LLMModel.enabled.is_(True), LLMProvider.enabled.is_(True))
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

    async def resolve(self, model_id: str) -> dict[str, str] | None:
        """Given an enabled model id, return its provider credentials (decrypted)."""
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.model_id == model_id,
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                    )
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        m, p = row
        return {"model_id": m.model_id, "api_key": self._dec(p.api_key), "api_base": p.api_base}


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
