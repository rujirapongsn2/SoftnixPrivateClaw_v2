"""Data access: append-only message store, memory store, users, audit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    bindparam,
    case,
    cast,
    false as sa_false,
    func,
    literal_column,
    or_,
    select,
    delete as sa_delete,
    update,
)
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from claw.core.plans import cost_allowed, cost_rank
from claw.db.models import (
    AppSetting,
    AuditEvent,
    ChatSession,
    Feedback,
    GuardrailRule,
    KnowledgeBase,
    KnowledgeBaseSharedGroup,
    KnowledgeChunk,
    KnowledgeDoc,
    LLMModel,
    LLMProvider,
    McpConnector,
    Memory,
    Message,
    PolicyPlan,
    Schedule,
    Share,
    Skill,
    SkillBundleVersion,
    SkillSubscription,
    UsageDaily,
    UsageRecord,
    User,
    UserGroup,
)


def _day_key(bucket_value: Any) -> str:
    """Normalize a date_trunc (datetime, Postgres) or strftime (str, SQLite)
    day bucket into a plain "YYYY-MM-DD" key."""
    return bucket_value.date().isoformat() if hasattr(bucket_value, "date") else str(bucket_value)


class MessageStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres

    async def append(self, session_id: str, entries: list[dict[str, Any]]) -> int:
        """Append turn messages atomically with monotonic per-session seq.

        Returns the seq of the first entry written (0 for an empty append), so a
        caller that has to enrich a message it already made durable can address
        it via set_content().
        """
        if not entries:
            return 0
        async with self.factory() as db:
            # Serialize sequence allocation across API and durable worker processes.
            await db.execute(
                update(ChatSession).where(ChatSession.id == session_id)
                .values(updated_at=datetime.now(timezone.utc))
            )
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
        return next_seq

    async def set_content(self, session_id: str, seq: int, content: str) -> None:
        """Rewrite one already-appended message's text.

        Exists so a message can be made durable before a slow step and enriched
        with that step's result afterwards, instead of being held unwritten
        while the step runs.
        """
        async with self.factory() as db:
            await db.execute(
                update(Message)
                .where(Message.session_id == session_id, Message.seq == seq)
                .values(content=content)
            )
            await db.commit()

    async def recent(self, session_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
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

    async def oldest_for_consolidation(
        self, session_id: str, *, after_seq: int, through_seq: int, limit: int
    ) -> list[dict[str, Any]]:
        """Oldest-first slice of a session's messages, for memory consolidation.

        Ascending, unlike `recent()`, and carrying each row's `seq`. Both matter
        for the same reason: consolidation advances a cursor past everything it
        reads, so it has to take the OLDEST unconsolidated messages and move the
        cursor to the last seq it actually summarized. Taking the newest N of a
        backlog larger than `limit` (what `recent()` returns) while moving the
        cursor to the end would strand every message before that window,
        permanently unsummarized.
        """
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == session_id,
                        Message.seq > after_seq,
                        Message.seq <= through_seq,
                    )
                    .order_by(Message.seq.asc())
                    .limit(limit)
                )
            ).all()
        return [{"seq": row.seq, "role": row.role, "content": row.content} for row in rows]

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
        bucket = (
            func.date_trunc("day", Message.created_at)
            if self.is_postgres
            else func.strftime("%Y-%m-%d", Message.created_at)
        )
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(bucket.label("day"), func.count())
                    .where(Message.created_at >= since, Message.role.in_(("user", "assistant")))
                    .group_by(bucket)
                )
            ).all()
        counts = {_day_key(r[0]): r[1] for r in rows if r[0] is not None}
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

    async def activity_by_day_hour(self, days: int = 7) -> list[dict[str, Any]]:
        """Dense message counts for each UTC hour across the latest calendar days."""
        today = datetime.now(timezone.utc).date()
        first_day = today - timedelta(days=days - 1)
        since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc)
        day_bucket = (
            func.date_trunc("day", Message.created_at)
            if self.is_postgres
            else func.strftime("%Y-%m-%d", Message.created_at)
        )
        hour_bucket = (
            func.extract("hour", Message.created_at)
            if self.is_postgres
            else func.strftime("%H", Message.created_at)
        )
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(day_bucket.label("day"), hour_bucket.label("hour"), func.count())
                    .where(Message.created_at >= since, Message.role.in_(("user", "assistant")))
                    .group_by(day_bucket, hour_bucket)
                )
            ).all()
        counts = {
            (_day_key(row[0]), int(row[1])): row[2]
            for row in rows
            if row[0] is not None and row[1] is not None
        }
        return [
            {"label": f"{day.isoformat()}T{hour:02d}", "count": counts.get((day.isoformat(), hour), 0)}
            for offset in range(days)
            for day in (first_day + timedelta(days=offset),)
            for hour in range(24)
        ]


class SessionStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres

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
            rows = await db.execute(select(ChatSession.user_id, func.count()).group_by(ChatSession.user_id))
            return {uid: n for uid, n in rows.all()}

    async def total(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(ChatSession))

    async def active_user_count(self, days: int = 7) -> int:
        """Distinct users with chat activity in the last `days` days."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.factory() as db:
            return (
                await db.scalar(
                    select(func.count(func.distinct(ChatSession.user_id))).where(
                        ChatSession.updated_at >= since
                    )
                )
                or 0
            )

    async def set_consolidated_seq(self, session_id: str, seq: int) -> None:
        """Advance the consolidation cursor, clearing the poison counter — the
        window that counter described is now behind the cursor either way."""
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.last_consolidated_seq = seq
                session.consolidation_failures = 0
                await db.commit()

    async def bump_consolidation_failures(self, session_id: str) -> int:
        """Record one unusable summarizer response for the current window and
        return the new consecutive count."""
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is None:
                return 0
            session.consolidation_failures = (session.consolidation_failures or 0) + 1
            count = session.consolidation_failures
            await db.commit()
            return count

    async def set_model(self, session_id: str, model: str | None) -> None:
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None and session.model != model:
                session.model = model
                await db.commit()

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        # `onupdate=_now` on `updated_at` fires for any UPDATE against this
        # table, Core or ORM, unless the column is explicitly given a value —
        # so self-assign it here to keep pinning from looking like activity
        # to the date-grouped sidebar.
        async with self.factory() as db:
            await db.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id)
                .values(pinned=pinned, updated_at=ChatSession.updated_at)
            )
            await db.commit()

    async def set_plan(self, session_id: str, goal: str, steps: list[dict[str, Any]]) -> None:
        """Replace the session's working plan (goal + ordered step checklist).

        Full-replace (not partial) so the agent sends its complete current plan
        each time — idempotent, no drift between stored and intended state.
        """
        async with self.factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.plan = {"goal": goal, "steps": steps}
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
        bucket = (
            func.date_trunc("day", ChatSession.created_at)
            if self.is_postgres
            else func.strftime("%Y-%m-%d", ChatSession.created_at)
        )
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(bucket.label("day"), func.count())
                    .where(ChatSession.created_at >= since)
                    .group_by(bucket)
                )
            ).all()
        counts = {_day_key(r[0]): r[1] for r in rows if r[0] is not None}
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
            row = await db.scalar(select(Memory).where(Memory.user_id == user_id, Memory.kind == "core"))
            return row.content if row else ""

    async def set_core(self, user_id: str, content: str) -> None:
        async with self.factory() as db:
            row = await db.scalar(select(Memory).where(Memory.user_id == user_id, Memory.kind == "core"))
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

    async def search_history(
        self, user_id: str, query: str, *, is_postgres: bool = True, limit: int = 8
    ) -> list[str]:
        """Best-matching consolidated history entries for a free-text query.

        These entries (one per consolidation pass) are the durable record of
        past conversations but aren't injected into context — this lets the
        agent pull the relevant ones back on demand. pg_trgm word-similarity on
        Postgres (language-agnostic, good for Thai/English, no embedding model);
        plain substring match on SQLite (tests).
        """
        query = (query or "").strip()
        if not query:
            return []
        like = f"%{query}%"
        async with self.factory() as db:
            if is_postgres:
                stmt = sa_text(
                    "SELECT content, word_similarity(:q, content) AS score "
                    "FROM memories "
                    "WHERE user_id = :uid AND kind = 'history' "
                    "AND (word_similarity(:q, content) > 0.12 OR content ILIKE :like) "
                    "ORDER BY score DESC LIMIT :limit"
                )
                rows = (
                    await db.execute(stmt, {"q": query, "uid": user_id, "like": like, "limit": limit})
                ).all()
                return [r[0] for r in rows]
            rows = (
                await db.execute(
                    select(Memory.content)
                    .where(
                        Memory.user_id == user_id,
                        Memory.kind == "history",
                        Memory.content.ilike(like),
                    )
                    .order_by(Memory.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [r[0] for r in rows]

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
            rows = await db.scalars(select(Skill).where(Skill.user_id == user_id).order_by(Skill.name))
            return list(rows)

    async def available_for_user(self, user_id: str) -> list[Skill]:
        """Own skills plus readable shares, annotated with recipient opt-ins."""
        async with self.factory() as db:
            viewer = await db.get(User, user_id)
            if viewer is None:
                return []
            shared_scope = Skill.visibility == "public"
            if viewer.group_id:
                shared_scope = or_(shared_scope, (Skill.visibility == "group") & (Skill.shared_group_id == viewer.group_id))
            rows = await db.scalars(
                select(Skill).join(User, User.id == Skill.user_id).where(
                    or_(Skill.user_id == user_id,
                        shared_scope & Skill.enabled.is_(True) & User.is_active.is_(True))
                ).order_by(Skill.name, Skill.created_at, Skill.id)
            )
            skills = list(rows)
            subscribed_ids = set(
                (await db.scalars(
                    select(SkillSubscription.skill_id).where(SkillSubscription.user_id == user_id)
                )).all()
            )
            for skill in skills:
                # The owner controls source availability; recipients separately
                # choose whether to include a shared skill in their AI context.
                skill.subscription_enabled = skill.enabled if skill.user_id == user_id else skill.id in subscribed_ids
            return skills

    async def enabled_for_user(self, user_id: str) -> list[Skill]:
        # Filter before resolving collisions: a disabled private copy must not
        # hide an enabled shared skill of the same name.
        rows = [s for s in await self.available_for_user(user_id) if getattr(s, "subscription_enabled", s.enabled)]
        # A private skill wins a name collision. Shared collisions resolve to
        # the oldest stable ID, identically for discovery and read_skill.
        rows.sort(key=lambda skill: skill.user_id != user_id)
        chosen = {}
        for skill in rows:
            chosen.setdefault(skill.name, skill)
        return list(chosen.values())

    async def readable_by_name(self, user_id: str, name: str) -> Skill | None:
        return next((s for s in await self.enabled_for_user(user_id) if s.name == name), None)

    async def get_by_name(self, user_id: str, name: str) -> Skill | None:
        async with self.factory() as db:
            return await db.scalar(select(Skill).where(Skill.user_id == user_id, Skill.name == name))

    async def upsert(self, user_id: str, name: str, **fields: Any) -> Skill:
        async with self.factory() as db:
            skill = await db.scalar(select(Skill).where(Skill.user_id == user_id, Skill.name == name))
            visibility = fields.get("visibility")
            if visibility is not None and visibility not in {"private", "group", "public"}:
                raise ValueError("invalid skill visibility")
            owner = None
            if visibility == "group":
                owner = await db.get(User, user_id)
                if owner is None or not owner.group_id:
                    raise ValueError("Join a group before sharing a skill with your group")
            if skill is None:
                skill = Skill(user_id=user_id, name=name)
                db.add(skill)
            if getattr(skill, "bundle_id", None) and any(key in fields and fields[key] != getattr(skill, key) for key in ("content", "description")):
                raise ValueError("Imported bundle instructions are read only")
            for key in ("description", "content", "enabled", "visibility"):
                if key in fields and fields[key] is not None:
                    setattr(skill, key, fields[key])
            if visibility == "group":
                # An explicit Group selection captures the group at this time;
                # ordinary content edits retain the original sharing target.
                skill.shared_group_id = owner.group_id
            elif visibility in {"private", "public"}:
                skill.shared_group_id = None
            # Unlike the fields above, connector_id's presence itself is the
            # signal (None is a valid, meaningful value — "no connector
            # linked" — not "leave unchanged"), so callers must omit the key
            # entirely to leave it alone.
            if "connector_id" in fields:
                skill.connector_id = fields["connector_id"]
            await db.commit()
            return skill

    async def set_subscription(self, user_id: str, skill_id: str, enabled: bool) -> bool:
        """Enable or remove a recipient opt-in after rechecking access."""
        async with self.factory() as db:
            viewer = await db.get(User, user_id)
            if viewer is None:
                return False
            shared_scope = Skill.visibility == "public"
            if viewer.group_id:
                shared_scope = or_(shared_scope, (Skill.visibility == "group") & (Skill.shared_group_id == viewer.group_id))
            skill = await db.scalar(
                select(Skill).join(User, User.id == Skill.user_id).where(
                    Skill.id == skill_id,
                    Skill.user_id != user_id,
                    shared_scope & Skill.enabled.is_(True) & User.is_active.is_(True),
                )
            )
            if skill is None:
                return False
            subscription = await db.get(SkillSubscription, {"user_id": user_id, "skill_id": skill_id})
            if enabled and subscription is None:
                db.add(SkillSubscription(user_id=user_id, skill_id=skill_id))
            elif not enabled and subscription is not None:
                await db.delete(subscription)
            await db.commit()
            return True

    async def delete(self, user_id: str, skill_id: str) -> bool:
        async with self.factory() as db:
            skill = await db.get(Skill, skill_id)
            if skill is None or skill.user_id != user_id:
                return False
            # Explicit dependent deletes keep the lifecycle correct even when
            # a SQLite deployment was created without FK enforcement. They
            # share this transaction with the registry row deletion.
            await db.execute(
                SkillSubscription.__table__.delete().where(SkillSubscription.skill_id == skill_id)
            )
            await db.execute(
                SkillBundleVersion.__table__.delete().where(SkillBundleVersion.skill_id == skill_id)
            )
            await db.delete(skill)
            await db.commit()
            return True


class ConnectorKindMismatch(Exception):
    """Raised by ConnectorStore.upsert when a caller's explicit ``kind`` differs
    from the stored row's. Checked inside the write transaction (not by a
    separate read beforehand) so two concurrent writers can't both see "no
    conflict" and then race each other's kind through — see
    claw/api/connector_shared.py's upsert_connector, which turns this into 422."""

    def __init__(self, existing_kind: str):
        self.existing_kind = existing_kind
        super().__init__(f"connector kind cannot be changed after creation (existing kind: {existing_kind})")


def _map_operation_bodies(operations: Any, transform: Callable[[str], str]) -> Any:
    """Apply `transform` to every operation's `body` template, leaving the rest
    of the operation JSON untouched.

    A kind="api" body template is a request body the cURL importer copies from
    whatever example request the user pasted, so it routinely carries a literal
    credential that has no BODY_* env equivalent to move it to. Only `body` is
    transformed: the other fields (name, method, path, parameter schema) are
    read by the admin UI and by ConnectorManager's tool-name collision check,
    and encrypting them would make the stored row unreadable for those.

    Tolerant of a malformed stored row — `operations` is JSON that can be
    hand-edited, and this runs on every connector read."""
    if not isinstance(operations, list):
        return operations
    out = []
    for op in operations:
        if isinstance(op, dict) and isinstance(op.get("body"), str) and op["body"]:
            op = {**op, "body": transform(op["body"])}
        out.append(op)
    return out


class ConnectorStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        # When set, connector env values and api-operation body templates are
        # encrypted at rest and decrypted on read.
        self.secret_box = secret_box

    def _decrypt(self, row: McpConnector) -> McpConnector:
        if self.secret_box is None:
            return row
        if row.env:
            row.env = self.secret_box.decrypt_map(row.env)
        if row.operations:
            # SecretBox.decrypt passes a non-prefixed value straight through,
            # so rows written before body encryption existed keep working
            # without a data migration.
            row.operations = _map_operation_bodies(row.operations, self.secret_box.decrypt)
        return row

    async def list_for_user(self, owner_id: str) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector).where(McpConnector.owner_id == owner_id).order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def enabled_for_user(self, owner_id: str) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector)
                .where(McpConnector.owner_id == owner_id, McpConnector.enabled.is_(True))
                .order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def list_for_global(self) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector).where(McpConnector.owner_id.is_(None)).order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def enabled_for_global(self) -> list[McpConnector]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector)
                .where(McpConnector.owner_id.is_(None), McpConnector.enabled.is_(True))
                .order_by(McpConnector.name)
            )
            return [self._decrypt(r) for r in rows]

    async def enabled_accessible(self, user_id: str) -> list[McpConnector]:
        """Enabled connectors this user can call: their own plus admin-global ones.

        A global connector is excluded if its ``name`` collides with one of the
        user's own — the user's own always wins, mirroring
        ``LLMConfigStore.resolve()``'s owner-vs-global tie-break.
        """
        async with self.factory() as db:
            rows = await db.scalars(
                select(McpConnector)
                .where(
                    or_(McpConnector.owner_id == user_id, McpConnector.owner_id.is_(None)),
                    McpConnector.enabled.is_(True),
                )
                # Prefer the caller's own connector (owner_id NOT NULL) on a name tie.
                .order_by(McpConnector.owner_id.is_(None))
            )
            all_rows = [self._decrypt(r) for r in rows]
        own_names = {r.name for r in all_rows if r.owner_id == user_id}
        result = [r for r in all_rows if r.owner_id == user_id or r.name not in own_names]
        result.sort(key=lambda r: r.name)
        return result

    async def get_by_name(self, owner_id: str | None, name: str) -> McpConnector | None:
        async with self.factory() as db:
            row = await db.scalar(
                select(McpConnector).where(McpConnector.owner_id == owner_id, McpConnector.name == name)
            )
            return self._decrypt(row) if row is not None else None

    async def upsert(self, owner_id: str | None, name: str, **fields: Any) -> McpConnector:
        # Retried once: a concurrent creator can win the (owner_id, name) unique
        # index between our select and commit, turning the insert into an
        # IntegrityError. The retry re-selects, finds the now-existing row, and
        # takes the update path — where the kind check below applies, so the
        # loser of that race is rejected rather than silently overwriting the
        # winner's kind.
        for attempt in (1, 2):
            try:
                return await self._upsert_once(owner_id, name, fields)
            except IntegrityError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    async def _upsert_once(self, owner_id: str | None, name: str, fields: dict[str, Any]) -> McpConnector:
        async with self.factory() as db:
            row = await db.scalar(
                select(McpConnector).where(McpConnector.owner_id == owner_id, McpConnector.name == name)
            )
            # A connector's kind is fixed at creation: flipping it would orphan
            # every existing reference to its old mcp_*/api_* tool names
            # (skills, tool_args_exempt globs). Enforced here, inside the same
            # transaction as the write, so it can't be raced. Callers that
            # legitimately don't care about kind (e.g. connector_oauth.py's
            # token refresh) omit the key and are unaffected.
            if row is not None and fields.get("kind") is not None and row.kind != fields["kind"]:
                raise ConnectorKindMismatch(row.kind)
            if row is None:
                row = McpConnector(owner_id=owner_id, name=name)
                db.add(row)
            # `description`/`timeout_ms`/`operations` are the only nullable
            # columns, so only those support `key in fields` (caller
            # explicitly passed `timeout_ms=None` to clear an override back to
            # "use the instance-wide default", or `operations=None` for a
            # kind="mcp" row). The rest are NOT NULL columns — for those,
            # `None` can't mean "clear it" (there's no valid empty state to
            # clear to), so they keep the older "explicit None == leave
            # untouched" behavior, same as omitting the kwarg entirely (e.g.
            # connector_oauth.py's preset install never passes these).
            nullable_keys = ("description", "timeout_ms", "operations")
            not_null_keys = ("kind", "transport", "command", "url", "env", "enabled")
            for key in nullable_keys:
                if key in fields:
                    value = fields[key]
                    if key == "operations" and self.secret_box is not None:
                        value = _map_operation_bodies(value, self.secret_box.encrypt)
                    setattr(row, key, value)
            for key in not_null_keys:
                if key in fields and fields[key] is not None:
                    value = fields[key]
                    if key == "env" and self.secret_box is not None:
                        value = self.secret_box.encrypt_map(value)
                    setattr(row, key, value)
            await db.commit()
            self._decrypt(row)  # return plaintext env to the caller
            return row

    async def delete(self, owner_id: str | None, connector_id: str) -> str | None:
        """Returns the deleted connector's name (so a global-scope caller can
        immediately tear down its live session — see ConnectorManager.
        close_global), or None if not found / not owned by `owner_id`."""
        async with self.factory() as db:
            row = await db.get(McpConnector, connector_id)
            if row is None or row.owner_id != owner_id:
                return None
            name = row.name
            await db.delete(row)
            await db.commit()
            return name

    async def delete_if_unchanged(
        self, owner_id: str | None, connector_id: str, expected_updated_at: datetime
    ) -> bool:
        """Atomically delete only the exact revision the caller inspected."""
        async with self.factory() as db:
            result = await db.execute(
                sa_delete(McpConnector).where(
                    McpConnector.id == connector_id,
                    McpConnector.owner_id == owner_id,
                    McpConnector.updated_at == expected_updated_at,
                )
            )
            await db.commit()
            return bool(result.rowcount)


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

    async def due(self, now: datetime, limit: int = 200) -> list[Schedule]:
        """Jobs ready to run, earliest deadline first. Bounded because this runs
        once a minute across every tenant: anything past the limit is still due
        next pass, so a backlog drains instead of arriving as one burst."""
        async with self.factory() as db:
            rows = await db.scalars(
                select(Schedule)
                .where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at <= now,
                )
                .order_by(Schedule.next_run_at.asc())
                .limit(max(1, limit))
            )
            return list(rows)

    async def mark_ran(self, schedule_id: str, *, next_run_at: datetime | None, status: str) -> None:
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

    async def settle_run(
        self,
        schedule_id: str,
        *,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        status: str,
    ) -> None:
        """Record a run without overwriting a schedule edited while it ran.

        The deadline is the run's revision token.  Keeping the comparison and
        both writes in one UPDATE closes the read/write race between the
        scheduler and the edit endpoint.
        """
        same_run = Schedule.next_run_at == expected_next_run_at
        completed_one_shot = (
            same_run & (Schedule.interval_seconds == 0) & (Schedule.cron == "")
            if next_run_at is None
            else sa_false()
        )
        async with self.factory() as db:
            await db.execute(
                update(Schedule)
                .where(Schedule.id == schedule_id)
                .values(
                    last_run_at=datetime.now(timezone.utc),
                    last_status=status[:300],
                    next_run_at=case(
                        (same_run, next_run_at),
                        else_=Schedule.next_run_at,
                    ),
                    enabled=case(
                        (completed_one_shot, False),
                        else_=Schedule.enabled,
                    ),
                )
            )
            await db.commit()


class UserStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get_or_create_by_email(
        self, email: str, display_name: str = "", signup_method: str = "dev_token"
    ) -> User:
        async with self.factory() as db:
            user = await db.scalar(select(User).where(func.lower(User.email) == email.lower()))
            if user is None:
                user = User(
                    email=email,
                    display_name=display_name or email.split("@")[0],
                    signup_method=signup_method,
                )
                db.add(user)
                await db.commit()
            return user

    async def get_by_email(self, email: str) -> User | None:
        """Case-insensitive lookup — email addresses aren't meaningfully
        case-sensitive in practice, and a bulk-imported row is normalized to
        lowercase (see admin.py's import_users_commit) while a person typing
        their own email rarely matches that exactly."""
        async with self.factory() as db:
            return await db.scalar(select(User).where(func.lower(User.email) == email.lower()))

    async def labels(self, ids: list[str]) -> dict[str, str]:
        """Map user ids → a display label (name, else email, else id) — for
        attaching human-readable names to id-keyed aggregates."""
        if not ids:
            return {}
        async with self.factory() as db:
            rows = (
                await db.execute(select(User.id, User.display_name, User.email).where(User.id.in_(ids)))
            ).all()
        return {uid: (name or email or uid) for uid, name, email in rows}

    async def display_names(self, ids: list[str]) -> dict[str, str]:
        """Safe display labels for user-shared resources; never expose email."""
        if not ids:
            return {}
        async with self.factory() as db:
            rows = (await db.execute(select(User.id, User.display_name).where(User.id.in_(ids)))).all()
        return {uid: name for uid, name in rows if name}

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
        signup_method: str = "password",
    ) -> User:
        async with self.factory() as db:
            user = User(
                email=email,
                display_name=display_name or email.split("@")[0],
                password_hash=password_hash,
                is_admin=is_admin,
                role=role,
                group_id=group_id,
                signup_method=signup_method,
            )
            db.add(user)
            await db.commit()
            return user

    async def existing_emails(self, emails: list[str]) -> set[str]:
        """Case-insensitive membership check for many emails in one round
        trip — lets a bulk import dedupe against the DB without a query per
        row. Returned emails are lowercased."""
        if not emails:
            return set()
        lowered = [e.lower() for e in emails]
        async with self.factory() as db:
            rows = await db.execute(select(User.email).where(func.lower(User.email).in_(lowered)))
            return {email.lower() for (email,) in rows.all()}

    async def bulk_create_imported(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        """Create many bulk-imported users in as few round trips as possible
        — one transaction for the whole batch in the common case. Returns a
        map of lowercased email -> status for every row that did NOT get
        created: "already_exists" for a genuine race against a concurrent
        insert (confirmed via a fresh lookup, not just assumed), or "error"
        for any other constraint violation (e.g. a bad group_id) — these
        must not be silently mislabeled as a duplicate."""
        if not rows:
            return {}
        async with self.factory() as db:
            for r in rows:
                db.add(User(**r))
            try:
                await db.commit()
                return {}
            except IntegrityError:
                await db.rollback()
        # Something in this batch failed — retry one at a time to isolate
        # exactly which row(s) failed and why, keeping the rest.
        failed: dict[str, str] = {}
        for r in rows:
            async with self.factory() as db:
                db.add(User(**r))
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    email = r["email"].lower()
                    failed[email] = (
                        "already_exists" if await self.get_by_email(email) is not None else "error"
                    )
        return failed

    async def assign_group(self, user_id: str, group_id: str | None) -> User | None:
        """Set (or clear, with None) a user's organizational group."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            user.group_id = group_id
            await db.commit()
            return user

    async def assign_plan(self, user_id: str, plan_id: str | None) -> User | None:
        """Set (or clear, with None) a user's usage-tier plan. None falls back
        to the user's group plan, then the system default (PolicyPlanStore.
        resolve_for_user)."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            user.plan_id = plan_id
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
            return await db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True)))

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
                await db.execute(Message.__table__.delete().where(Message.session_id.in_(session_ids)))
            # Private (BYOK) providers and their models are owned via owner_id.
            provider_ids = (
                await db.scalars(select(LLMProvider.id).where(LLMProvider.owner_id == user_id))
            ).all()
            if provider_ids:
                await db.execute(LLMModel.__table__.delete().where(LLMModel.provider_id.in_(provider_ids)))
                await db.execute(LLMProvider.__table__.delete().where(LLMProvider.owner_id == user_id))
            # Private (non-shared) connectors are likewise owned via owner_id —
            # an admin-global connector (owner_id NULL) is never touched here.
            await db.execute(McpConnector.__table__.delete().where(McpConnector.owner_id == user_id))
            for model in (ChatSession, Memory, Skill, Schedule, UsageRecord, UsageDaily, Feedback):
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

    async def update_preferences(
        self,
        user_id: str,
        *,
        ui_language: str | None = None,
        font_size: str | None = None,
        chat_background: str | None = None,
        execution_panel_enabled: bool | None = None,
    ) -> User | None:
        """Settings > Profile > Preferences — a personal override of the
        Control Plane's global branding defaults. All three are stored null
        until the user's first save (see BrandingStore for the global
        fallback these are layered on top of in the frontend). Each field is
        independent: a None here means "leave this field's stored override
        alone", not "clear it" — mirrors update_flags/update_profile above so
        saving one field never wipes the other two back to null."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            if ui_language is not None:
                user.ui_language = ui_language
            if font_size is not None:
                user.font_size = font_size
            if chat_background is not None:
                user.chat_background = chat_background
            if execution_panel_enabled is not None:
                user.execution_panel_enabled = execution_panel_enabled
            await db.commit()
            return user

    async def claim_activation_send(self, user_id: str, now: datetime, cooldown_seconds: int) -> bool:
        """Atomically claim the right to send an imported-user activation
        email right now, enforcing the resend cooldown at the DB layer
        instead of a read-then-write race. A plain "read timestamp, decide,
        send, then write timestamp" sequence lets N concurrent callers (e.g.
        an attacker firing repeated /login attempts for a known imported
        email) all observe the same stale timestamp and all send — this
        conditional UPDATE means only one caller's WHERE clause matches at a
        time. Returns True if this call won the claim (stamps
        activation_email_sent_at and should proceed to send), False if
        another call already claimed it within the cooldown window."""
        cutoff = now - timedelta(seconds=cooldown_seconds)
        async with self.factory() as db:
            result = await db.execute(
                update(User)
                .where(
                    User.id == user_id,
                    or_(User.activation_email_sent_at.is_(None), User.activation_email_sent_at < cutoff),
                )
                .values(activation_email_sent_at=now)
            )
            await db.commit()
            return result.rowcount > 0

    async def claim_password_reset_send(
        self, user_id: str, now: datetime, cooldown_seconds: int, nonce: str
    ) -> bool:
        """Atomically claim the right to send a "forgot password" reset email
        right now (same cooldown-at-the-DB-layer reasoning as
        claim_activation_send) AND record the nonce that will be embedded in
        the emailed token, so redeem_password_reset() can later enforce
        single-use via compare-and-swap. Returns True if this call won the
        claim (stamps password_reset_sent_at/password_reset_nonce and should
        proceed to send), False if another call already claimed it within
        the cooldown window."""
        cutoff = now - timedelta(seconds=cooldown_seconds)
        async with self.factory() as db:
            result = await db.execute(
                update(User)
                .where(
                    User.id == user_id,
                    or_(User.password_reset_sent_at.is_(None), User.password_reset_sent_at < cutoff),
                )
                .values(password_reset_sent_at=now, password_reset_nonce=nonce)
            )
            await db.commit()
            return result.rowcount > 0

    async def redeem_password_reset(self, user_id: str, nonce: str, password_hash: str) -> bool:
        """Atomically consume a password-reset token: sets the new password
        hash and clears the nonce in one compare-and-swap UPDATE, matched on
        the nonce embedded in the emailed token. Returns True if the nonce
        matched (a real, not-yet-redeemed, not-superseded-by-a-newer-request
        token) AND the account is currently active, and the password was
        set; False otherwise — the same single query is the validity check,
        the single-use enforcement, AND the suspension gate, so there's no
        window where a suspended account's password gets written before a
        separate "is it active" check catches up (the write simply never
        happens for a suspended row, full stop)."""
        async with self.factory() as db:
            result = await db.execute(
                update(User)
                .where(User.id == user_id, User.password_reset_nonce == nonce, User.is_active.is_(True))
                .values(password_hash=password_hash, password_reset_nonce=None)
            )
            await db.commit()
            return result.rowcount > 0

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
                prior = await db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
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
            await db.execute(User.__table__.update().where(User.group_id == group_id).values(group_id=None))
            # Drop any explicit knowledge-base shares into this group too —
            # same "don't rely on ON DELETE CASCADE" reasoning as above.
            await db.execute(
                KnowledgeBaseSharedGroup.__table__.delete().where(
                    KnowledgeBaseSharedGroup.group_id == group_id
                )
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

    async def set_plan(self, group_id: str, plan_id: str | None) -> UserGroup | None:
        """Set (or clear, with None) the group's default usage-tier plan."""
        async with self.factory() as db:
            row = await db.get(UserGroup, group_id)
            if row is None:
                return None
            row.plan_id = plan_id
            await db.commit()
            return row

    async def default_group(self) -> UserGroup | None:
        async with self.factory() as db:
            return await db.scalar(select(UserGroup).where(UserGroup.is_default.is_(True)).limit(1))

    async def counts_by_group(self) -> dict[str, int]:
        """user_id count per group_id (excludes ungrouped)."""
        async with self.factory() as db:
            rows = await db.execute(
                select(User.group_id, func.count()).where(User.group_id.is_not(None)).group_by(User.group_id)
            )
            return {gid: n for gid, n in rows.all()}


_PLAN_FIELDS = (
    "name",
    "rank",
    "max_chat_cost",
    "allow_image",
    "max_image_cost",
    "messages_per_day",
    "images_per_day",
    "turns_per_minute",
)


def plan_to_dict(p: PolicyPlan) -> dict[str, Any]:
    """Serialize a PolicyPlan to the plain dict used everywhere off the ORM
    (API responses, the resolve cache, enforcement)."""
    return {
        "id": p.id,
        "name": p.name,
        "rank": p.rank,
        "max_chat_cost": p.max_chat_cost,
        "allow_image": p.allow_image,
        "max_image_cost": p.max_image_cost,
        "messages_per_day": p.messages_per_day,
        "images_per_day": p.images_per_day,
        "turns_per_minute": p.turns_per_minute,
        "is_default": p.is_default,
    }


class PolicyPlanStore:
    """Usage-tier plans: model cost ceilings + daily/per-minute quotas, assigned
    per-user or per-group. Plans change rarely but are read on every turn, so
    the effective-plan lookup is served from a small in-process cache that is
    invalidated on any write (mirrors PolicyEngine's reload model)."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        # {"by_id": {id: dict}, "default": dict | None}; None = not yet loaded.
        self._cache: dict[str, Any] | None = None

    def _invalidate(self) -> None:
        self._cache = None

    async def _cached(self) -> dict[str, Any]:
        if self._cache is None:
            async with self.factory() as db:
                rows = list(await db.scalars(select(PolicyPlan)))
            by_id = {p.id: plan_to_dict(p) for p in rows}
            default = next((plan_to_dict(p) for p in rows if p.is_default), None)
            self._cache = {"by_id": by_id, "default": default}
        return self._cache

    async def list(self) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = await db.scalars(select(PolicyPlan).order_by(PolicyPlan.rank, PolicyPlan.name))
            return [plan_to_dict(p) for p in rows]

    async def get(self, plan_id: str) -> dict[str, Any] | None:
        async with self.factory() as db:
            row = await db.get(PolicyPlan, plan_id)
            return plan_to_dict(row) if row else None

    async def count(self) -> int:
        async with self.factory() as db:
            return await db.scalar(select(func.count()).select_from(PolicyPlan))

    async def create(self, **fields: Any) -> PolicyPlan:
        want_default = bool(fields.pop("is_default", False))
        async with self.factory() as db:
            if want_default:
                await db.execute(PolicyPlan.__table__.update().values(is_default=False))
            row = PolicyPlan(**{k: v for k, v in fields.items() if k in _PLAN_FIELDS})
            row.is_default = want_default
            db.add(row)
            await db.commit()
            await db.refresh(row)
        self._invalidate()
        return row

    async def update(self, plan_id: str, **fields: Any) -> PolicyPlan | None:
        async with self.factory() as db:
            row = await db.get(PolicyPlan, plan_id)
            if row is None:
                return None
            for key in _PLAN_FIELDS:
                if key in fields and fields[key] is not None:
                    setattr(row, key, fields[key])
            if fields.get("is_default") is True:
                await db.execute(
                    PolicyPlan.__table__.update().where(PolicyPlan.id != plan_id).values(is_default=False)
                )
                row.is_default = True
            elif fields.get("is_default") is False:
                if row.is_default:
                    # Clearing the sole default would leave every user/group
                    # without an explicit plan_id silently unrestricted (their
                    # resolve_for_user() falls through to no plan at all).
                    # Require picking a replacement default first via
                    # set_default() instead of allowing a bare unset.
                    other_default = (
                        await db.execute(
                            select(PolicyPlan.id)
                            .where(PolicyPlan.id != plan_id, PolicyPlan.is_default.is_(True))
                            .limit(1)
                        )
                    ).first()
                    if other_default is None:
                        raise ValueError(
                            "cannot unset the only default plan — set another plan as default first"
                        )
                row.is_default = False
            await db.commit()
            await db.refresh(row)
        self._invalidate()
        return row

    async def delete(self, plan_id: str) -> bool:
        """Delete a plan; users/groups referencing it become plan-less (plan_id
        → NULL). Done explicitly so it works where the FK's ON DELETE isn't
        enforced (SQLite in tests), matching GroupStore.delete."""
        async with self.factory() as db:
            row = await db.get(PolicyPlan, plan_id)
            if row is None:
                return False
            await db.execute(User.__table__.update().where(User.plan_id == plan_id).values(plan_id=None))
            await db.execute(
                UserGroup.__table__.update().where(UserGroup.plan_id == plan_id).values(plan_id=None)
            )
            await db.delete(row)
            await db.commit()
        self._invalidate()
        return True

    async def set_default(self, plan_id: str | None) -> None:
        """Make `plan_id` the sole default plan, or clear the default when None."""
        async with self.factory() as db:
            await db.execute(PolicyPlan.__table__.update().values(is_default=False))
            if plan_id is not None:
                row = await db.get(PolicyPlan, plan_id)
                if row is not None:
                    row.is_default = True
            await db.commit()
        self._invalidate()

    async def default_plan(self) -> dict[str, Any] | None:
        return (await self._cached())["default"]

    async def resolve_for_user(self, user_id: str) -> dict[str, Any] | None:
        """Effective plan for a user: their own plan → their group's default
        plan → the system default plan. None means no plan applies (no
        restriction / unlimited) — keeps the feature non-breaking."""
        cache = await self._cached()
        by_id = cache["by_id"]
        if not by_id:
            return None
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(User.plan_id, UserGroup.plan_id)
                    .select_from(User)
                    .join(UserGroup, User.group_id == UserGroup.id, isouter=True)
                    .where(User.id == user_id)
                )
            ).first()
        if row is not None:
            plan_id = row[0] or row[1]
            if plan_id and plan_id in by_id:
                return by_id[plan_id]
        return cache["default"]

    async def resolve_for_users(self, user_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Batched resolve_for_user() for a known set of ids — one query
        instead of one round-trip per user (e.g. the admin overview's top-N
        usage list), same resolution order (user plan → group plan → default)."""
        if not user_ids:
            return {}
        cache = await self._cached()
        by_id = cache["by_id"]
        if not by_id:
            return {uid: None for uid in user_ids}
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(User.id, User.plan_id, UserGroup.plan_id)
                    .select_from(User)
                    .join(UserGroup, User.group_id == UserGroup.id, isouter=True)
                    .where(User.id.in_(user_ids))
                )
            ).all()
        found = {row[0]: (row[1] or row[2]) for row in rows}
        return {
            uid: by_id.get(found.get(uid) or "", cache["default"]) if uid in found else cache["default"]
            for uid in user_ids
        }

    async def counts_by_plan(self) -> dict[str, int]:
        """Direct-assignment user count per plan_id (excludes users covered only
        via their group's plan — those are attributed to the group, not counted
        here)."""
        async with self.factory() as db:
            rows = await db.execute(
                select(User.plan_id, func.count()).where(User.plan_id.is_not(None)).group_by(User.plan_id)
            )
            return {pid: n for pid, n in rows.all()}

    async def seed(self, plans: list[dict[str, Any]]) -> None:
        """Insert built-in plans once, when the table is empty."""
        async with self.factory() as db:
            for p in plans:
                db.add(PolicyPlan(**p))
            await db.commit()
        self._invalidate()


class UsageStore:
    _GRANULARITIES = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}

    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres

    async def record(
        self,
        user_id: str,
        session_id: str | None,
        model: str,
        usage: dict[str, int],
        count_turn: bool = True,
        count_plan_turn: bool = True,
        metrics: dict[str, int] | None = None,
    ) -> None:
        """Record token spend. `count_turn=False` for background work (memory
        consolidation): the tokens are real and belong in the bill, but they are
        not a chat turn the user took. It is persisted on the raw row as well as
        applied to the rollup, because every turn figure has to agree — the
        reports continue to use UsageDaily.turns. UsageDaily.plan_turns is the
        plan-quota subset and excludes My Models turns when
        `count_plan_turn=False`.

        `metrics` carries the turn's shape (iterations, tool_calls, ttft_ms,
        duration_ms) onto the raw row only — the daily rollup stays a pure cost
        table, since averaging latency across a day×user×model bucket would
        lose the distribution that makes the number worth having."""
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        # A caller with turn-shape metrics to report (a real chat turn) still
        # has something worth writing even if the provider reported no token
        # usage (some OpenAI-compatible backends omit usage on a streamed
        # response) — only skip the write when there is truly nothing: no
        # tokens and no metrics, which is the pre-metrics behavior memory
        # consolidation's caller (no `metrics` arg) still relies on.
        if prompt == 0 and completion == 0 and metrics is None:
            return
        shape = metrics or {}
        today = datetime.now(timezone.utc).date()
        model = model or ""
        # Write the raw per-turn row AND fold it into today's rollup bucket in
        # one transaction. Portable upsert: try UPDATE, else INSERT. The only
        # race is two concurrent FIRST inserts of the same (day,user,model) —
        # one hits the unique index; catch it and retry (the UPDATE then wins).
        for attempt in range(2):
            try:
                async with self.factory() as db:
                    db.add(
                        UsageRecord(
                            user_id=user_id,
                            session_id=session_id,
                            model=model,
                            prompt_tokens=prompt,
                            completion_tokens=completion,
                            counts_as_turn=count_turn,
                            iterations=int(shape.get("iterations", 0) or 0),
                            tool_calls=int(shape.get("tool_calls", 0) or 0),
                            ttft_ms=int(shape.get("ttft_ms", 0) or 0),
                            duration_ms=int(shape.get("duration_ms", 0) or 0),
                        )
                    )
                    res = await db.execute(
                        update(UsageDaily)
                        .where(
                            UsageDaily.day == today,
                            UsageDaily.user_id == user_id,
                            UsageDaily.model == model,
                        )
                        .values(
                            prompt_tokens=UsageDaily.prompt_tokens + prompt,
                            completion_tokens=UsageDaily.completion_tokens + completion,
                            turns=UsageDaily.turns + (1 if count_turn else 0),
                            plan_turns=UsageDaily.plan_turns
                            + (1 if count_turn and count_plan_turn else 0),
                        )
                    )
                    if res.rowcount == 0:
                        db.add(
                            UsageDaily(
                                day=today,
                                user_id=user_id,
                                model=model,
                                prompt_tokens=prompt,
                                completion_tokens=completion,
                                turns=1 if count_turn else 0,
                                plan_turns=1 if count_turn and count_plan_turn else 0,
                            )
                        )
                    await db.commit()
                return
            except IntegrityError:
                if attempt == 1:
                    raise
                # Another turn created the bucket first; retry so the UPDATE path hits.
                continue

    async def record_image(self, user_id: str, model: str) -> None:
        """Increment today's image-generation count for a user. The /images
        path emits no tokens, so record() never sees it — this keeps the
        images/day plan quota fed. Same portable UPDATE-else-INSERT upsert +
        retry as record()."""
        today = datetime.now(timezone.utc).date()
        model = model or ""
        for attempt in range(2):
            try:
                async with self.factory() as db:
                    res = await db.execute(
                        update(UsageDaily)
                        .where(
                            UsageDaily.day == today,
                            UsageDaily.user_id == user_id,
                            UsageDaily.model == model,
                        )
                        .values(images=UsageDaily.images + 1)
                    )
                    if res.rowcount == 0:
                        db.add(UsageDaily(day=today, user_id=user_id, model=model, images=1))
                    await db.commit()
                return
            except IntegrityError:
                if attempt == 1:
                    raise
                continue

    async def top_users_today(self, limit: int = 15) -> list[dict[str, int]]:
        """Users nearest their Plan quotas today, bounded to ``limit`` rows."""
        today = datetime.now(timezone.utc).date()
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        UsageDaily.user_id,
                        func.coalesce(func.sum(UsageDaily.plan_turns), 0).label("turns"),
                        func.coalesce(func.sum(UsageDaily.images), 0).label("images"),
                    )
                    .where(UsageDaily.day == today)
                    .group_by(UsageDaily.user_id)
                    .order_by(func.sum(UsageDaily.plan_turns).desc())
                    .limit(limit)
                )
            ).all()
        return [
            {"user_id": uid, "turns": int(turns or 0), "images": int(images or 0)}
            for uid, turns, images in rows
        ]

    async def release_image(self, user_id: str, model: str) -> None:
        """Undo a record_image() reservation (decrement, floored at 0). Used
        when an image slot was reserved up front but generation was then
        rejected (over quota) or failed."""
        today = datetime.now(timezone.utc).date()
        model = model or ""
        async with self.factory() as db:
            await db.execute(
                update(UsageDaily)
                .where(
                    UsageDaily.day == today,
                    UsageDaily.user_id == user_id,
                    UsageDaily.model == model,
                    UsageDaily.images > 0,
                )
                .values(images=UsageDaily.images - 1)
            )
            await db.commit()

    async def usage_today(self, user_id: str) -> dict[str, int]:
        """Today's total usage plus the chat-turn subset charged to a plan."""
        today = datetime.now(timezone.utc).date()
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UsageDaily.turns), 0),
                        func.coalesce(func.sum(UsageDaily.images), 0),
                        func.coalesce(func.sum(UsageDaily.plan_turns), 0),
                    ).where(UsageDaily.day == today, UsageDaily.user_id == user_id)
                )
            ).one()
        return {
            "turns": int(row[0] or 0),
            "images": int(row[1] or 0),
            "plan_turns": int(row[2] or 0),
        }

    # Label format per granularity — used to normalize a PG bucket (a
    # datetime, period start) into the same shape SQLite's strftime-based
    # bucketing already yields directly.
    _BUCKET_LABEL_FMT = {"day": "%Y-%m-%d", "week": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}

    def _bucket_column(self, trunc: str):
        """SQL bucket expression for `trunc`, portable across dialects.

        Postgres: date_trunc handles every granularity uniformly. SQLite has
        no native truncation function — day/month/year bucket via strftime's
        format string alone (grouping identical formatted strings is the
        truncation); week has no format-string equivalent, so it walks back
        to the preceding Monday via day-of-week arithmetic (%w: 0=Sun..6=Sat)
        to match date_trunc('week', ...)'s ISO (Monday-start) semantics."""
        if self.is_postgres:
            return func.date_trunc(trunc, cast(UsageDaily.day, DateTime)).label("bucket")
        if trunc == "week":
            dow = cast(func.strftime("%w", UsageDaily.day), Integer)
            offset = (dow + 6) % 7
            modifier = literal_column("'-'") + cast(offset, String) + literal_column("' days'")
            return func.date(UsageDaily.day, modifier).label("bucket")
        fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[trunc]
        return func.strftime(fmt, UsageDaily.day).label("bucket")

    def _format_bucket(self, b: Any, trunc: str) -> str:
        """PG yields a datetime (period start) that needs trimming to the
        granularity's label shape; SQLite's bucketing already yields the
        label string directly."""
        if hasattr(b, "date"):
            return b.date().strftime(self._BUCKET_LABEL_FMT[trunc])
        return str(b)

    async def token_series(
        self,
        *,
        granularity: str,
        start: "date",
        end: "date",
        group_col: str,
        user_id: str | None = None,
        models: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Rollup token totals bucketed by time period and one dimension.

        `group_col` is "user_id" (User view) or "model" (Model view — the
        Provider view instead uses `token_series_by_user_model` since a
        model_id's provider can differ per user). Reads usage_daily only, so
        cost scales with days×users×models in range, not raw turn volume."""
        trunc = self._GRANULARITIES.get(granularity, "day")
        key_col = UsageDaily.user_id if group_col == "user_id" else UsageDaily.model
        conds = [UsageDaily.day >= start, UsageDaily.day <= end]
        if user_id:
            conds.append(UsageDaily.user_id == user_id)
        if models is not None:
            if not models:
                return []
            conds.append(UsageDaily.model.in_(models))

        bucket = self._bucket_column(trunc)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        bucket,
                        key_col.label("key"),
                        func.coalesce(func.sum(UsageDaily.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageDaily.completion_tokens), 0),
                        func.coalesce(func.sum(UsageDaily.turns), 0),
                    )
                    .where(*conds)
                    .group_by(bucket, key_col)
                )
            ).all()
        return [
            {
                "bucket": self._format_bucket(b, trunc),
                "key": key or "",
                "prompt_tokens": p,
                "completion_tokens": c,
                "turns": t,
            }
            for b, key, p, c, t in rows
        ]

    async def token_series_by_user_model(
        self,
        *,
        granularity: str,
        start: "date",
        end: "date",
        user_id: str | None = None,
        models: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Like token_series but grouped by (bucket, user_id, model) instead
        of one dimension — the Provider view needs this because a model_id's
        provider can differ per user (each BYOK user configures their own),
        so folding by model_id alone would misattribute one user's usage to
        a different user's private provider."""
        trunc = self._GRANULARITIES.get(granularity, "day")
        conds = [UsageDaily.day >= start, UsageDaily.day <= end]
        if user_id:
            conds.append(UsageDaily.user_id == user_id)
        if models is not None:
            if not models:
                return []
            conds.append(UsageDaily.model.in_(models))

        bucket = self._bucket_column(trunc)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        bucket,
                        UsageDaily.user_id,
                        UsageDaily.model,
                        func.coalesce(func.sum(UsageDaily.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageDaily.completion_tokens), 0),
                        func.coalesce(func.sum(UsageDaily.turns), 0),
                    )
                    .where(*conds)
                    .group_by(bucket, UsageDaily.user_id, UsageDaily.model)
                )
            ).all()
        return [
            {
                "bucket": self._format_bucket(b, trunc),
                "user_id": uid or "",
                "model": model or "",
                "prompt_tokens": p,
                "completion_tokens": c,
                "turns": t,
            }
            for b, uid, model, p, c, t in rows
        ]

    async def distinct_user_ids(self, start: "date | None" = None, end: "date | None" = None) -> list[str]:
        """User ids with rollup activity, optionally date-bounded — scopes
        cross-tenant BYOK provider/model lookups to users who actually have
        usage instead of scanning every registered account."""
        conds = []
        if start is not None:
            conds.append(UsageDaily.day >= start)
        if end is not None:
            conds.append(UsageDaily.day <= end)
        async with self.factory() as db:
            rows = await db.execute(select(UsageDaily.user_id).where(*conds).distinct())
            return [r[0] for r in rows.all() if r[0]]

    # Rows written by background work carry counts_as_turn=False, so a plain
    # count() over usage_records would report more turns than the quota counts.
    _TURNS = func.coalesce(func.sum(case((UsageRecord.counts_as_turn, 1), else_=0)), 0)

    async def totals(self) -> dict[str, int]:
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
                        self._TURNS,
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
                        self._TURNS,
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
                        self._TURNS,
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
    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres

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
            stmt = stmt.where(func.cast(AuditEvent.payload, String).ilike(like) | AuditEvent.kind.ilike(like))
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
        async with self.factory() as db:
            if self.is_postgres:
                bucket = func.date_trunc("day", AuditEvent.created_at)
                rows = (
                    await db.execute(
                        select(bucket.label("day"), func.count())
                        .where(AuditEvent.kind == "policy", AuditEvent.created_at >= since)
                        .group_by(bucket)
                    )
                ).all()
                counts = {r[0].date().isoformat(): r[1] for r in rows if r[0] is not None}
            else:
                # SQLite fallback (tests/dev): strftime already yields the ISO label.
                bucket = func.strftime("%Y-%m-%d", AuditEvent.created_at)
                rows = (
                    await db.execute(
                        select(bucket.label("day"), func.count())
                        .where(AuditEvent.kind == "policy", AuditEvent.created_at >= since)
                        .group_by(bucket)
                    )
                ).all()
                counts = {r[0]: r[1] for r in rows if r[0] is not None}
        today = datetime.now(timezone.utc).date()
        return [
            {"label": (d := (today - timedelta(days=i)).isoformat()), "count": counts.get(d, 0)}
            for i in range(days - 1, -1, -1)
        ]

    async def policy_hits_by_user(self, days: int = 14, limit: int = 15) -> list[dict[str, Any]]:
        """Top users by guardrail-match count over the last `days` days, highest
        first — for the Safety tab's "hits by user" breakdown."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(AuditEvent.user_id, func.count())
                    .where(AuditEvent.kind == "policy", AuditEvent.created_at >= since)
                    .group_by(AuditEvent.user_id)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
            ).all()
        return [{"user_id": uid or "", "count": count} for uid, count in rows]

    async def policy_hits_by_rule(self, days: int = 14, limit: int = 15) -> list[dict[str, Any]]:
        """Top guardrail rules by match count over the last `days` days, highest
        first. Each policy hit's payload carries `rules: [name, …]` (a turn can
        match more than one rule), so this unnests that array before counting."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.factory() as db:
            if self.is_postgres:
                rows = (
                    await db.execute(
                        sa_text(
                            "SELECT rule, count(*) AS n FROM audit_events, "
                            "json_array_elements_text(COALESCE(payload->'rules', '[]')) AS rule "
                            "WHERE kind = 'policy' AND created_at >= :since "
                            "GROUP BY rule ORDER BY n DESC LIMIT :limit"
                        ),
                        {"since": since, "limit": limit},
                    )
                ).all()
                return [{"rule": rule, "count": count} for rule, count in rows]
            # SQLite fallback (tests/dev): tally in Python — guardrail hits are a
            # low-volume security signal, not a per-turn event, so this is cheap.
            events = (
                await db.execute(
                    select(AuditEvent.payload).where(
                        AuditEvent.kind == "policy", AuditEvent.created_at >= since
                    )
                )
            ).all()
            tally: dict[str, int] = {}
            for (payload,) in events:
                for rule in (payload or {}).get("rules", []):
                    tally[rule] = tally.get(rule, 0) + 1
            ranked = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:limit]
            return [{"rule": rule, "count": count} for rule, count in ranked]


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
    @staticmethod
    def _providers_query():
        return select(LLMProvider).order_by(LLMProvider.name)

    async def list_providers(self, owner_id: str | None = None) -> list[LLMProvider]:
        async with self.factory() as db:
            rows = await db.scalars(self._providers_query().where(LLMProvider.owner_id == owner_id))
            return list(rows)

    async def list_all_providers(self, owner_ids: Sequence[str] = ()) -> list[LLMProvider]:
        """Admin-global providers plus the given owners' BYOK providers — the
        deliberate, sole cross-tenant exception in this store (every other
        method here stays scoped to one `owner_id`), used only for read-only
        analytics (e.g. the admin Tokens Usage report's provider attribution),
        never for CRUD. Always bounded to owners known to have activity
        (callers pass `UsageStore.distinct_user_ids()`), never a blanket scan
        of every registered account — pass no `owner_ids` to get admin-global
        providers only."""
        async with self.factory() as db:
            rows = await db.scalars(
                self._providers_query().where(
                    or_(LLMProvider.owner_id.is_(None), LLMProvider.owner_id.in_(owner_ids))
                )
            )
            return list(rows)

    async def get_by_name(self, name: str, owner_id: str | None = None) -> LLMProvider | None:
        async with self.factory() as db:
            return await db.scalar(
                select(LLMProvider).where(LLMProvider.name == name, LLMProvider.owner_id == owner_id)
            )

    async def create_provider(
        self,
        name: str,
        api_key: str,
        api_base: str,
        enabled: bool = True,
        model_prefix: str = "",
        owner_id: str | None = None,
        auto_disable_models: bool = False,
    ) -> LLMProvider:
        async with self.factory() as db:
            row = LLMProvider(
                name=name,
                api_key=self._enc(self._clean_key(api_key)),
                api_base=api_base,
                enabled=enabled,
                model_prefix=model_prefix,
                owner_id=owner_id,
                auto_disable_models=auto_disable_models if owner_id is None else False,
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
            route_changed = bool(fields.get("api_key"))
            route_changed = route_changed or (
                "api_base" in fields and fields["api_base"] is not None and fields["api_base"] != row.api_base
            )
            route_changed = route_changed or (fields.get("enabled") is True and not row.enabled)
            route_changed = route_changed or (
                owner_id is None and fields.get("auto_disable_models") is True and not row.auto_disable_models
            )
            if "name" in fields and fields["name"]:
                row.name = fields["name"]
            if "api_base" in fields and fields["api_base"] is not None:
                row.api_base = fields["api_base"]
            if "enabled" in fields and fields["enabled"] is not None:
                row.enabled = fields["enabled"]
            if "model_prefix" in fields and fields["model_prefix"] is not None:
                row.model_prefix = fields["model_prefix"]
            if owner_id is None and fields.get("auto_disable_models") is not None:
                row.auto_disable_models = fields["auto_disable_models"]
            # Only overwrite the key when a non-empty value is supplied.
            if fields.get("api_key"):
                row.api_key = self._enc(self._clean_key(fields["api_key"]))
            if route_changed:
                await db.execute(
                    LLMModel.__table__.update()
                    .where(LLMModel.provider_id == provider_id)
                    .values(
                        health_status="unchecked", health_reason="",
                        health_checked_at=None, health_claim_until=None,
                    )
                )
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
    @staticmethod
    def _models_query():
        return (
            select(LLMModel)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .order_by(LLMModel.label)
        )

    async def list_models(self, owner_id: str | None = None) -> list[LLMModel]:
        async with self.factory() as db:
            rows = await db.scalars(self._models_query().where(LLMProvider.owner_id == owner_id))
            return list(rows)

    async def list_all_models(self, owner_ids: Sequence[str] = ()) -> list[LLMModel]:
        """Models under admin-global providers plus the given owners' BYOK
        providers — paired with list_all_providers(); same bounded,
        analytics-only, names-only contract."""
        async with self.factory() as db:
            rows = await db.scalars(
                self._models_query().where(
                    or_(LLMProvider.owner_id.is_(None), LLMProvider.owner_id.in_(owner_ids))
                )
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
        kind: str = "chat",
        owner_id: str | None = None,
        context_window: int | None = None,
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
                kind=kind or "chat",
                context_window=context_window or None,
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
            for key in ("model_id", "label", "enabled", "cost", "description", "kind"):
                if key in fields and fields[key] is not None:
                    setattr(row, key, fields[key])
            if fields.get("enabled") is True or "model_id" in fields:
                row.health_auto_disabled = False
                row.health_status = "unchecked"
                row.health_reason = ""
                row.health_checked_at = None
                row.health_claim_until = None
            # 0 (or any falsy value) clears the override back to "look it up",
            # since there is no such thing as a zero-token window.
            if "context_window" in fields:
                row.context_window = fields["context_window"] or None
            # Only a chat model can be the global default. Reclassifying the
            # current default to "image" (or any non-chat kind) must clear its
            # is_default — otherwise default_model() (which filters kind=="chat")
            # would find no default and the deployment loses its chat default.
            if row.kind != "chat":
                row.is_default = False
                row.is_fallback = False
            # Keep the fallback assignment when disabled so the Control Plane
            # can warn which route needs an administrator's attention. Runtime
            # fallback_model_for() still excludes disabled models/providers.
            # The auto-selected default is an admin-global concept only; private
            # models are never the global default (owner_id=None gates it).
            elif owner_id is None and fields.get("is_default"):
                # Exactly one default across all global models.
                await db.execute(
                    LLMModel.__table__.update()
                    .where(
                        LLMModel.provider_id.in_(select(LLMProvider.id).where(LLMProvider.owner_id.is_(None))),
                        LLMModel.id != row.id,
                    )
                    .values(is_default=False)
                )
                row.is_default = True
                # The normal route and its backstop must be different models.
                row.is_fallback = False
            elif owner_id is None and fields.get("is_default") is False:
                row.is_default = False
            if owner_id is None and fields.get("is_fallback"):
                if row.enabled and row.kind == "chat" and not row.is_default:
                    await db.execute(
                        LLMModel.__table__.update()
                        .where(
                            LLMModel.provider_id.in_(
                                select(LLMProvider.id).where(LLMProvider.owner_id.is_(None))
                            ),
                            LLMModel.id != row.id,
                        )
                        .values(is_fallback=False)
                    )
                    row.is_fallback = True
            elif owner_id is None and fields.get("is_fallback") is False:
                row.is_fallback = False
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
    async def enabled_models(
        self, user_id: str | None = None, kind: str = "chat", max_cost: str | None = None
    ) -> list[dict[str, Any]]:
        """Enabled models of the given kind ("chat" for the agent picker,
        "image" for the text-to-image picker): admin-global models merged with
        the caller's own private (BYOK) models. Each carries a ``scope`` marker
        ("global" | "private") so the UI can badge the user's own. Chat and
        image models are intentionally separate lists — an image model can't
        do tool calling, so it must never appear as a chat option.

        ``max_cost`` is the caller's usage-plan cost ceiling (None = no limit):
        admin-global models above it are dropped, but the caller's own BYOK
        models are always kept — they pay for their own key, so the plan tier
        (which meters the operator's shared models) doesn't restrict them."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        LLMModel.kind == kind,
                        or_(LLMProvider.auto_disable_models.is_(False), LLMModel.health_status.notin_(("quarantined", "unavailable"))),
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
            # BYOK (owner's own) rows bypass the plan ceiling; global rows are gated.
            if p.owner_id is not None or cost_allowed(max_cost, m.cost)
        ]

    async def distinct_chat_models(self, limit: int = 200) -> list[tuple[str, int | None]]:
        """Distinct enabled chat model ids with their context-window override
        (None = no override), admin-global and BYOK together.

        Read-only and deliberately unscoped by owner (like `list_all_providers`)
        because it answers a deployment-wide question: which models is the app
        able to look up metadata for. DISTINCT + LIMIT keeps the result bounded
        no matter how many users bring their own keys.
        """
        async with self.factory() as db:
            rows = await db.execute(
                select(LLMModel.model_id, LLMModel.context_window)
                .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                .where(
                    LLMModel.enabled.is_(True),
                    LLMProvider.enabled.is_(True),
                    LLMModel.kind == "chat",
                )
                .distinct()
                .order_by(LLMModel.model_id)
                .limit(limit)
            )
            return [(m, w) for m, w in rows]

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
                        LLMModel.kind == "chat",
                        or_(LLMProvider.auto_disable_models.is_(False), LLMModel.health_status.notin_(("quarantined", "unavailable"))),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row.model_id if row else None

    async def has_configured_global_chat_models(self) -> bool:
        """Distinguish a fresh env-only install from a disabled DB model lineup."""
        async with self.factory() as db:
            row = await db.scalar(
                select(LLMModel.id)
                .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                .where(LLMProvider.owner_id.is_(None), LLMModel.kind == "chat")
                .limit(1)
            )
            return row is not None

    async def default_model_for(self, max_cost: str | None = None) -> str | None:
        """Return an allowed global default, or None when its configured row is
        disabled. A plan that excludes an enabled default may use another
        allowed model; an unavailable default requires manual selection."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(LLMModel.kind == "chat", LLMProvider.owner_id.is_(None))
                )
            ).all()
        configured_default = next(((m, p) for m, p in rows if m.is_default), None)
        # An unavailable default is an explicit stop: require the user to pick
        # a replacement instead of silently routing to the highest cost model.
        if configured_default and (
            not configured_default[0].enabled or not configured_default[1].enabled
            or (configured_default[1].auto_disable_models and configured_default[0].health_status in {"quarantined", "unavailable"})
        ):
            return None
        allowed = [
            m for m, p in rows
            if m.enabled and p.enabled and (not p.auto_disable_models or m.health_status not in {"quarantined", "unavailable"})
            and cost_allowed(max_cost, m.cost)
        ]
        if not allowed:
            return None
        default = next((m for m in allowed if m.is_default), None)
        if default is not None:
            return default.model_id
        # No allowed default → most capable allowed tier wins.
        return max(allowed, key=lambda m: cost_rank(m.cost)).model_id

    async def fallback_model_for(self, max_cost: str | None = None) -> str | None:
        """Return the configured global fallback when it is enabled, is a chat
        model, and fits the caller's plan. There is no implicit substitute: an
        admin who leaves fallback unset has explicitly disabled failover."""
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        LLMProvider.owner_id.is_(None),
                        LLMModel.kind == "chat",
                        LLMModel.is_fallback.is_(True),
                        or_(LLMProvider.auto_disable_models.is_(False), LLMModel.health_status.notin_(("quarantined", "unavailable"))),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None or not cost_allowed(max_cost, row.cost):
            return None
        return row.model_id

    async def resolve(
        self, model_id: str, user_id: str | None = None, max_cost: str | None = None
    ) -> dict[str, Any] | None:
        """Given an enabled model id, return its provider credentials (decrypted).

        Scope: admin-global providers plus the caller's own private ones. If the
        same model id exists in both scopes, the user's own wins (ordered first) —
        so a user's key is used for their model and one user can never resolve
        another user's credentials. Only matches kind="chat" — an image-only
        model must never be selectable as a chat turn's model (it can't do tool
        calling), mirroring resolve_image()'s reverse guard.

        ``max_cost`` (usage-plan ceiling) hard-denies a global model above the
        tier by returning None — defense in depth so a client can't bypass the
        cost-filtered picker by passing a disallowed model_id directly. The
        caller's own BYOK model is never gated (they pay for it)."""
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.model_id == model_id,
                        LLMModel.kind == "chat",
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        or_(LLMProvider.auto_disable_models.is_(False), LLMModel.health_status.notin_(("quarantined", "unavailable"))),
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
        # Plan ceiling gates admin-global models only; the caller's own BYOK is exempt.
        if p.owner_id is None and not cost_allowed(max_cost, m.cost):
            return None
        # Sanitize on read too, so keys stored before sanitization existed (or
        # any stray whitespace) can't crash the outbound HTTP header encoding.
        return {
            "model_id": m.model_id,
            "api_key": self._clean_key(self._dec(p.api_key)),
            "api_base": p.api_base,
            "context_window": m.context_window,
            "scope": "private" if p.owner_id else "global",
        }

    async def resolve_image(
        self, model_id: str, user_id: str | None = None, max_cost: str | None = None
    ) -> dict[str, str] | None:
        """Like resolve(), but ONLY matches models classified kind="image", and
        also returns the provider's model_prefix so the image path can pick its
        generation strategy (openai/azure -> /images endpoint; else -> chat
        multimodal). Same owner-scoping and own-provider-wins tiebreak as
        resolve(), so this can never reach another user's credentials, and a
        chat model id can never be driven through the image path. ``max_cost``
        hard-denies a global image model above the plan's image ceiling (BYOK
        exempt), mirroring resolve()."""
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.model_id == model_id,
                        LLMModel.kind == "image",
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        or_(
                            LLMProvider.owner_id.is_(None),
                            LLMProvider.owner_id == user_id,
                        ),
                    )
                    .order_by(LLMProvider.owner_id.is_(None))
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        m, p = row
        if p.owner_id is None and not cost_allowed(max_cost, m.cost):
            return None
        return {
            "model_id": m.model_id,
            "api_key": self._clean_key(self._dec(p.api_key)),
            "api_base": p.api_base or "",
            "model_prefix": p.model_prefix or "",
            "scope": "private" if p.owner_id else "global",
        }

    async def resolve_vision(self, user_id: str | None = None) -> dict[str, Any] | None:
        """The kind="vision" model to hand an image to when the chat model can't
        read one, or None when the operator configured no such model.

        Takes no model_id: the user never picks this one — it is the operator's
        designated reader, selected here. Preference order is the caller's own
        (BYOK) provider first, then the cheapest tier: this call only has to
        describe an image, so spending the top tier on it by default would
        quietly inflate every image turn. is_default is deliberately not
        consulted — update_model() forces it False on every non-chat kind, so
        it can never be set on a vision row.

        The plan's max_chat_cost ceiling deliberately does NOT apply here. It
        gates which model the user may *converse* with; this reader is the
        operator's own, and applying the chat ceiling to it meant the seeded
        default plan ("low") hid every medium-tier reader — silently disabling
        delegation for exactly the users most likely to be on a text-only
        model. Cost stays bounded by the cheapest-tier preference below.

        Same owner-scoping as resolve(), so one user can never reach another's
        credentials. The row fetch is bounded because the WHERE clause already
        narrows to admin-global rows plus this one caller's own."""
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(LLMModel, LLMProvider)
                    .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                    .where(
                        LLMModel.kind == "vision",
                        LLMModel.enabled.is_(True),
                        LLMProvider.enabled.is_(True),
                        or_(
                            LLMProvider.owner_id.is_(None),
                            LLMProvider.owner_id == user_id,
                        ),
                    )
                    .limit(50)
                )
            ).all()
        if not rows:
            return None
        m, p = min(
            rows,
            key=lambda row: (
                row[1].owner_id is None,  # False(0) for the caller's own → first
                cost_rank(row[0].cost),
            ),
        )
        return {
            "model_id": m.model_id,
            "api_key": self._clean_key(self._dec(p.api_key)),
            "api_base": p.api_base or "",
            "scope": "private" if p.owner_id else "global",
        }


class GuardrailStore:
    """Persists control-policy rules + the monitor-only toggle + the tool-args
    exemption list."""

    _MONITOR_KEY = "guardrail_monitor_only"
    _EXEMPT_KEY = "guardrail_tool_args_exempt"

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
            for key in (
                "name",
                "pattern",
                "action",
                "scopes",
                "placeholder",
                "severity",
                "block_message",
                "enabled",
            ):
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

    async def get_tool_args_exempt(self, default: list[str]) -> list[str]:
        """Tool-name globs exempt from tool_args masking. Returns `default` (the
        built-in list) until an admin has customized it."""
        async with self.factory() as db:
            row = await db.get(AppSetting, self._EXEMPT_KEY)
            if row is None or "value" not in (row.value or {}):
                return list(default)
            value = row.value.get("value")
            return list(value) if isinstance(value, list) else list(default)

    async def set_tool_args_exempt(self, globs: list[str]) -> None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._EXEMPT_KEY)
            if row is None:
                db.add(AppSetting(key=self._EXEMPT_KEY, value={"value": globs}))
            else:
                row.value = {"value": globs}
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


class ProjectContainerConfigStore:
    """Database-backed global switch; the environment is the initial fallback."""

    _KEY = "project_containers"
    _INGRESS_KEY = "project_public_ingress"
    _ACCESS_KEY = "project_internal_access"

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def get(
        self, default_enabled: bool = False, default_public_ingress_enabled: bool = False,
        default_host_bind_ip: str = "127.0.0.1",
    ) -> dict[str, bool | str]:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            ingress_row = await db.get(AppSetting, self._INGRESS_KEY)
            access_row = await db.get(AppSetting, self._ACCESS_KEY)
        value = dict(row.value or {}) if row is not None else {}
        ingress_value = dict(ingress_row.value or {}) if ingress_row is not None else {}
        access_value = dict(access_row.value or {}) if access_row is not None else {}
        enabled = value.get("enabled", default_enabled)
        # Read the original combined-row field as a migration fallback. New
        # writes use an independent row, so simultaneous admin toggles cannot
        # overwrite one another through JSON read-modify-write races.
        public_ingress_enabled = ingress_value.get(
            "enabled", value.get("public_ingress_enabled", default_public_ingress_enabled)
        )
        return {
            "enabled": enabled if isinstance(enabled, bool) else default_enabled,
            "public_ingress_enabled": (
                public_ingress_enabled if isinstance(public_ingress_enabled, bool)
                else default_public_ingress_enabled
            ),
            "host_bind_ip": (
                access_value.get("host_bind_ip")
                if isinstance(access_value.get("host_bind_ip"), str)
                else default_host_bind_ip
            ),
        }

    async def _set(self, setting_key: str, enabled: bool) -> None:
        async with self.factory() as db:
            row = await db.get(AppSetting, setting_key)
            # Preserve legacy fields on the container row until every install
            # has performed at least one ingress write to the new row.
            value = dict(row.value or {}) if row is not None and setting_key == self._KEY else {}
            value["enabled"] = enabled
            if row is None:
                db.add(AppSetting(key=setting_key, value=value))
            else:
                row.value = value
            await db.commit()

    async def set_enabled(self, enabled: bool) -> None:
        await self._set(self._KEY, enabled)

    async def set_public_ingress_enabled(self, enabled: bool) -> None:
        await self._set(self._INGRESS_KEY, enabled)

    async def set_host_bind_ip(self, host_bind_ip: str) -> None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._ACCESS_KEY)
            value = {"host_bind_ip": host_bind_ip}
            if row is None:
                db.add(AppSetting(key=self._ACCESS_KEY, value=value))
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


class SmtpConfigStore:
    """Admin-configured SMTP settings for transactional email (currently:
    the imported-user activation link, see claw/api/auth.py) — self-service
    from the Control Plane, same shape as TelegramConfigStore. The password
    is encrypted at rest (same scheme as OAuthAppStore/TelegramConfigStore).

    ``get()`` returns None when nobody has ever saved a config — callers must
    treat that as "email sending disabled". Unlike Telegram, there is no env
    var fallback: this is a brand-new capability with no legacy deployment
    to stay compatible with.
    """

    _KEY = "smtp_config"
    _DEFAULTS: dict[str, Any] = {
        "provider": "",
        "host": "",
        "port": 587,
        "username": "",
        "password": "",
        "from_address": "",
        "use_tls": True,
        "use_ssl": False,
        "enabled": False,
    }

    def __init__(self, factory: async_sessionmaker[AsyncSession], secret_box: Any | None = None):
        self.factory = factory
        self.secret_box = secret_box

    async def get(self) -> dict[str, Any] | None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
        if row is None:
            return None
        data = {**self._DEFAULTS, **(row.value or {})}
        password = data.get("password", "")
        if self.secret_box and password:
            password = self.secret_box.decrypt(password)
        data["password"] = password
        return data

    async def public(self) -> dict[str, Any]:
        """Safe view for the admin UI — never returns the password itself."""
        data = await self.get()
        if data is None:
            return {**self._DEFAULTS, "password": None, "has_password": False}
        pub = {k: v for k, v in data.items() if k != "password"}
        pub["has_password"] = bool(data["password"])
        return pub

    async def set(
        self,
        *,
        provider: str,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool,
        use_ssl: bool,
        enabled: bool,
    ) -> None:
        # Preserve the existing password when the caller submits a blank one
        # (the admin UI does this whenever it isn't changing the password).
        existing = await self.get()
        pwd = password or (existing["password"] if existing else "")
        stored_password = self.secret_box.encrypt(pwd) if (self.secret_box and pwd) else pwd
        value = {
            "provider": provider,
            "host": host,
            "port": port,
            "username": username,
            "password": stored_password,
            "from_address": from_address,
            "use_tls": use_tls,
            "use_ssl": use_ssl,
            "enabled": enabled,
        }
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            if row is None:
                db.add(AppSetting(key=self._KEY, value=value))
            else:
                row.value = value
            await db.commit()


class ArtifactJobStore:
    """Durable checkpoints for normal-chat artifact jobs.

    Stored in ``app_settings`` to keep this recovery feature migration-free.
    Each job owns one row and updates it by replacement, which works on both
    SQLite and Postgres JSON columns without mutable-value tracking surprises.
    """

    _PREFIX = "artifact_job:"

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    def _key(self, job_id: str) -> str:
        return f"{self._PREFIX}{job_id}"

    async def resource_policy(self) -> dict[str, Any]:
        async with self.factory() as db:
            row = await db.get(AppSetting, "team_resource_policy")
            return dict((row.value or {}).get("policy") or {}) if row else {}

    async def resumable(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        jobs = await self.list_for_session(user_id, session_id, active_only=False)
        return next((job for job in reversed(jobs)
                     if job.get("status") in {"blocked", "limit_reached"}), None)

    async def create(self, value: dict[str, Any]) -> dict[str, Any]:
        async with self.factory() as db:
            db.add(AppSetting(key=self._key(str(value["id"])), value=dict(value)))
            await db.commit()
        return dict(value)

    async def get(self, job_id: str) -> dict[str, Any] | None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._key(job_id))
        return dict(row.value) if row is not None else None

    async def update(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        async with self.factory() as db:
            row = await db.get(AppSetting, self._key(job_id))
            if row is None:
                return None
            value = {**(row.value or {}), **changes, "updated_at": datetime.now(timezone.utc).isoformat()}
            row.value = value
            await db.commit()
        return dict(value)

    async def finish(self, job_id: str, status: str, **changes: Any) -> dict[str, Any] | None:
        """Set a terminal state and discard sensitive recovery payloads."""
        if status == "limit_reached":
            return await self.update(job_id, status=status, **changes)
        return await self.update(
            job_id,
            status=status,
            content="",
            media=[],
            checkpoint_messages=[],
            tool_results={},
            pending_call=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
            **changes,
        )

    async def claim_recovery(self, job_id: str) -> dict[str, Any] | None:
        """Atomically claim one recoverable job across concurrent app workers."""
        async with self.factory() as db:
            row = await db.scalar(
                select(AppSetting)
                .where(AppSetting.key == self._key(job_id))
                .with_for_update()
            )
            if row is None or (row.value or {}).get("status") not in {"queued", "running", "waiting_dependency", "blocked"}:
                return None
            value = {
                **(row.value or {}),
                "status": "recovering",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            row.value = value
            await db.commit()
        return dict(value)

    async def prune_finished(self, max_age: timedelta = timedelta(days=1)) -> int:
        """Delete old terminal metadata so app_settings does not grow forever."""
        cutoff = datetime.now(timezone.utc) - max_age
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(AppSetting).where(
                        AppSetting.key.like(f"{self._PREFIX}%"),
                        AppSetting.updated_at < cutoff,
                    )
                )
            ).all()
            terminal = {"completed", "cancelled", "failed", "limit_reached"}
            doomed = [row for row in rows if (row.value or {}).get("status") in terminal]
            for row in doomed:
                await db.delete(row)
            if doomed:
                await db.commit()
        return len(doomed)

    async def list_recoverable(self) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(AppSetting).where(
                        AppSetting.key.like(f"{self._PREFIX}%")
                    )
                )
            ).all()
        return [
            dict(row.value)
            for row in rows
            if (row.value or {}).get("status") in {"queued", "running", "waiting_dependency", "blocked"}
        ]

    async def list_for_session(
        self, user_id: str, session_id: str, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(AppSetting).where(
                        AppSetting.key.like(f"{self._PREFIX}%")
                    )
                )
            ).all()
        jobs = [
            dict(row.value)
            for row in rows
            if (row.value or {}).get("user_id") == user_id
            and (row.value or {}).get("session_id") == session_id
            and (
                not active_only
                or (row.value or {}).get("status") in {
                    "queued", "running", "recovering", "cancelling", "waiting_dependency", "blocked"
                }
            )
        ]
        return sorted(jobs, key=lambda item: str(item.get("created_at") or ""))


class BrandingStore:
    """Admin-configured global branding & appearance (Control Plane >
    Preferences): logos for the three surfaces, UI/AI language, font size, and
    chat background. Global (one value for every user — no per-user override),
    self-service from the Control Plane. Same AppSetting key/value shape as
    SmtpConfigStore; nothing here is secret, so no secret_box.

    ``get()`` always returns a full dict (defaults merged over any stored row),
    so callers never have to special-case a fresh install — an unset install
    just returns the built-in defaults (English, small font, solid background,
    no custom logos → the frontend falls back to the bundled logo).

    Logo fields hold an opaque on-disk filename (e.g. ``login-a1b2c3d4.png``)
    written under settings.branding_root, NOT the image bytes — see
    claw/api/admin.py for upload/serve. None ⇒ use the bundled default logo.

    ``get()`` is backed by a small in-process cache (populated on first read,
    replaced on every write below) — it's hit on every chat turn and every
    page load's /api/branding fetch, so a per-call DB round trip would be
    wasted work for a value that changes only when an admin saves Preferences.
    This assumes a single worker process (see scripts/claw: uvicorn runs
    without --workers), matching how PolicyEngine's live rule set is already
    cached in-process rather than reloaded per request.
    """

    _KEY = "branding"
    LANGUAGES = ("en", "th")
    FONT_SIZES = ("small", "medium", "large")
    CHAT_BACKGROUNDS = ("solid", "dots", "grid")
    LOGO_SLOTS = ("login", "chat", "sidebar")
    _DEFAULTS: dict[str, Any] = {
        "language": "en",
        "font_size": "small",
        "chat_background": "solid",
        "logo_login": None,
        "logo_chat": None,
        "logo_sidebar": None,
    }

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        self._cache: dict[str, Any] | None = None

    async def get(self) -> dict[str, Any]:
        if self._cache is None:
            async with self.factory() as db:
                row = await db.get(AppSetting, self._KEY)
            self._cache = {**self._DEFAULTS, **(row.value if row else {})}
        return dict(self._cache)  # copy: callers must not mutate the cache

    async def set(
        self,
        *,
        language: str,
        font_size: str,
        chat_background: str,
    ) -> dict[str, Any]:
        """Update the non-logo preferences (logos are managed by set_logo /
        clear_logo so an image upload and a preference change stay independent).
        Enum values are validated here as a defense-in-depth backstop even though
        the API layer also constrains them with Literal types."""
        if language not in self.LANGUAGES:
            raise ValueError(f"invalid language: {language!r}")
        if font_size not in self.FONT_SIZES:
            raise ValueError(f"invalid font_size: {font_size!r}")
        if chat_background not in self.CHAT_BACKGROUNDS:
            raise ValueError(f"invalid chat_background: {chat_background!r}")
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            current = {**self._DEFAULTS, **(row.value if row else {})}
            current["language"] = language
            current["font_size"] = font_size
            current["chat_background"] = chat_background
            if row is None:
                db.add(AppSetting(key=self._KEY, value=current))
            else:
                row.value = current
            await db.commit()
            self._cache = dict(current)
            return current

    async def set_logo(self, slot: str, filename: str) -> dict[str, Any]:
        """Point a logo slot at a newly-uploaded on-disk filename. Returns the
        previous filename for that slot (or None) so the caller can delete the
        stale file after committing."""
        if slot not in self.LOGO_SLOTS:
            raise ValueError(f"invalid logo slot: {slot!r}")
        key = f"logo_{slot}"
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            current = {**self._DEFAULTS, **(row.value if row else {})}
            previous = current.get(key)
            current[key] = filename
            if row is None:
                db.add(AppSetting(key=self._KEY, value=current))
            else:
                row.value = current
            await db.commit()
            self._cache = dict(current)
        return previous

    async def clear_logo(self, slot: str) -> str | None:
        """Reset a logo slot to the bundled default. Returns the removed
        filename (or None) so the caller can delete the file from disk."""
        if slot not in self.LOGO_SLOTS:
            raise ValueError(f"invalid logo slot: {slot!r}")
        key = f"logo_{slot}"
        async with self.factory() as db:
            row = await db.get(AppSetting, self._KEY)
            if row is None:
                return None
            current = {**self._DEFAULTS, **(row.value or {})}
            previous = current.get(key)
            current[key] = None
            row.value = current
            await db.commit()
            self._cache = dict(current)
        return previous


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
    _VISIBILITIES = ("private", "group", "public")

    async def create_base(
        self, owner_id: str, name: str, description: str = "", visibility: str = "private",
        kind: str = "general",
    ) -> KnowledgeBase:
        async with self.factory() as db:
            kb = KnowledgeBase(
                owner_id=owner_id,
                name=name[:120],
                description=description,
                visibility=visibility if visibility in self._VISIBILITIES else "private",
                kind=kind if kind in {"general", "queryable"} else "general",
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
            requested_kind = fields.get("kind")
            if requested_kind in {"general", "queryable"} and requested_kind != kb.kind:
                doc_count = await db.scalar(
                    select(func.count(KnowledgeDoc.id)).where(KnowledgeDoc.kb_id == kb_id)
                )
                if doc_count:
                    raise ValueError("knowledge type cannot be changed after files are uploaded")
                kb.kind = requested_kind
            if fields.get("visibility") in self._VISIBILITIES:
                kb.visibility = fields["visibility"]
                if kb.visibility != "group":
                    # Explicit shares are only meaningful under "group" visibility.
                    await db.execute(
                        KnowledgeBaseSharedGroup.__table__.delete().where(
                            KnowledgeBaseSharedGroup.kb_id == kb_id
                        )
                    )
            await db.commit()
            await db.refresh(kb)
            return kb

    async def set_shared_groups(self, kb_id: str, group_ids: list[str]) -> None:
        """Replace the set of additional groups a `group`-visibility base is
        shared with (the owner's own group is always included and never
        stored here — see KnowledgeBase.visibility)."""
        async with self.factory() as db:
            await db.execute(
                KnowledgeBaseSharedGroup.__table__.delete().where(KnowledgeBaseSharedGroup.kb_id == kb_id)
            )
            for group_id in dict.fromkeys(group_ids):  # dedupe, preserve order
                db.add(KnowledgeBaseSharedGroup(kb_id=kb_id, group_id=group_id))
            await db.commit()

    async def owner_group_id(self, owner_id: str) -> str | None:
        async with self.factory() as db:
            owner = await db.get(User, owner_id)
            return owner.group_id if owner is not None else None

    async def shared_group_ids(self, kb_id: str) -> list[str]:
        async with self.factory() as db:
            rows = (
                (
                    await db.execute(
                        select(KnowledgeBaseSharedGroup.group_id).where(
                            KnowledgeBaseSharedGroup.kb_id == kb_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def list_accessible(self, user_id: str) -> list[dict[str, Any]]:
        """Bases the user can see — their own, all public, and any `group`
        base whose owner shares the viewer's *current* group (default or
        explicitly shared) — each with a doc count."""
        owner = aliased(User)
        async with self.factory() as db:
            viewer = await db.get(User, user_id)
            viewer_group_id = viewer.group_id if viewer is not None else None
            group_clause = self._group_visible_clause(owner, viewer_group_id)
            stmt = (
                select(KnowledgeBase, owner.group_id)
                .join(owner, owner.id == KnowledgeBase.owner_id)
                .outerjoin(
                    KnowledgeBaseSharedGroup,
                    (KnowledgeBaseSharedGroup.kb_id == KnowledgeBase.id)
                    & (KnowledgeBaseSharedGroup.group_id == viewer_group_id),
                )
                .where(
                    (KnowledgeBase.owner_id == user_id)
                    | (KnowledgeBase.visibility == "public")
                    | group_clause
                )
                .distinct()
                .order_by(KnowledgeBase.updated_at.desc())
            )
            rows = (await db.execute(stmt)).all()
            counts = dict(
                (
                    await db.execute(select(KnowledgeDoc.kb_id, func.count()).group_by(KnowledgeDoc.kb_id))
                ).all()
            )
            group_names = {g.id: g.name for g in (await db.execute(select(UserGroup))).scalars().all()}
            # Explicit shares are only the caller's own business to see —
            # one bounded bulk query over just their own group-visibility
            # bases, not per-row.
            own_group_kb_ids = [
                kb.id for kb, _ in rows if kb.owner_id == user_id and kb.visibility == "group"
            ]
            shared_by_kb: dict[str, list[str]] = {}
            if own_group_kb_ids:
                for row in await db.execute(
                    select(KnowledgeBaseSharedGroup.kb_id, KnowledgeBaseSharedGroup.group_id).where(
                        KnowledgeBaseSharedGroup.kb_id.in_(own_group_kb_ids)
                    )
                ):
                    shared_by_kb.setdefault(row.kb_id, []).append(row.group_id)
        return [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description,
                "visibility": kb.visibility,
                "kind": kb.kind,
                "owner_id": kb.owner_id,
                "is_owner": kb.owner_id == user_id,
                "owner_group_name": group_names.get(owner_group_id),
                "shared_group_ids": shared_by_kb.get(kb.id, []) if kb.owner_id == user_id else [],
                "docs": int(counts.get(kb.id, 0)),
                "updated_at": kb.updated_at.isoformat(),
            }
            for kb, owner_group_id in rows
        ]

    async def accessible_ids(self, user_id: str) -> list[str]:
        owner = aliased(User)
        async with self.factory() as db:
            viewer = await db.get(User, user_id)
            viewer_group_id = viewer.group_id if viewer is not None else None
            group_clause = self._group_visible_clause(owner, viewer_group_id)
            stmt = (
                select(KnowledgeBase.id)
                .join(owner, owner.id == KnowledgeBase.owner_id)
                .outerjoin(
                    KnowledgeBaseSharedGroup,
                    (KnowledgeBaseSharedGroup.kb_id == KnowledgeBase.id)
                    & (KnowledgeBaseSharedGroup.group_id == viewer_group_id),
                )
                .where(
                    (KnowledgeBase.owner_id == user_id)
                    | (KnowledgeBase.visibility == "public")
                    | group_clause
                )
                .distinct()
            )
            rows = (await db.execute(stmt)).scalars().all()
        return list(rows)

    @staticmethod
    def _group_visible_clause(owner: Any, viewer_group_id: str | None):
        """`group`-visibility match: the viewer has a group, and either the
        owner's *current* group is the viewer's group (default share) or an
        explicit KnowledgeBaseSharedGroup row exists for the viewer's group
        (joined in by the caller, filtered to viewer_group_id already)."""
        if viewer_group_id is None:
            return sa_false()
        return (KnowledgeBase.visibility == "group") & (
            (owner.group_id == viewer_group_id) | (KnowledgeBaseSharedGroup.group_id == viewer_group_id)
        )

    async def delete_base(self, kb_id: str) -> None:
        async with self.factory() as db:
            await db.execute(KnowledgeChunk.__table__.delete().where(KnowledgeChunk.kb_id == kb_id))
            await db.execute(KnowledgeDoc.__table__.delete().where(KnowledgeDoc.kb_id == kb_id))
            # Explicit cleanup rather than relying on the FK's ON DELETE CASCADE
            # — SQLite (tests) doesn't enforce it without a pragma, same reason
            # GroupStore.delete() clears User.group_id explicitly.
            await db.execute(
                KnowledgeBaseSharedGroup.__table__.delete().where(KnowledgeBaseSharedGroup.kb_id == kb_id)
            )
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
        chunk_records: list[tuple[int | None, str]],
    ) -> KnowledgeDoc:
        """Persist a document and its chunks. `chunk_records` is a list of
        (page, text) — page is the 1-based PDF page or None for paged-less
        formats; it enriches citations and never affects retrieval."""
        async with self.factory() as db:
            doc = KnowledgeDoc(
                kb_id=kb_id,
                concept_id=concept_id,
                title=title[:255],
                filename=filename[:255],
                mime=mime[:120],
                size=size,
                chars=chars,
                chunks=len(chunk_records),
            )
            db.add(doc)
            await db.flush()
            for i, (page, text) in enumerate(chunk_records):
                db.add(
                    KnowledgeChunk(kb_id=kb_id, doc_id=doc.id, seq=i, title=title[:255], text=text, page=page)
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

    # -- background ingestion (queue) ---------------------------------------
    async def create_pending_doc(
        self, *, kb_id: str, title: str, filename: str, mime: str, size: int
    ) -> KnowledgeDoc:
        """Register an uploaded document before it is parsed. The background
        worker fills in concept_id/chars/chunks and flips status to ready."""
        async with self.factory() as db:
            doc = KnowledgeDoc(
                kb_id=kb_id,
                concept_id="",
                title=title[:255],
                filename=filename[:255],
                mime=mime[:120],
                size=size,
                chars=0,
                chunks=0,
                status="pending",
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            return doc

    async def finalize_doc(
        self,
        *,
        doc_id: str,
        concept_id: str,
        chars: int,
        chunk_records: list[tuple[int | None, str]],
    ) -> KnowledgeDoc | None:
        """Attach parsed chunks to a pending doc and mark it ready."""
        async with self.factory() as db:
            doc = await db.get(KnowledgeDoc, doc_id)
            if doc is None:
                return None
            # Clear any prior chunks (e.g. a retried ingest) before re-inserting.
            await db.execute(KnowledgeChunk.__table__.delete().where(KnowledgeChunk.doc_id == doc_id))
            for i, (page, text) in enumerate(chunk_records):
                db.add(
                    KnowledgeChunk(
                        kb_id=doc.kb_id, doc_id=doc.id, seq=i, title=doc.title, text=text, page=page
                    )
                )
            doc.concept_id = concept_id
            doc.chars = chars
            doc.chunks = len(chunk_records)
            doc.status = "ready"
            doc.error = ""
            kb = await db.get(KnowledgeBase, doc.kb_id)
            if kb is not None:
                kb.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(doc)
            return doc

    async def finalize_dataset_doc(
        self,
        *,
        doc_id: str,
        dataset_path: str,
        dataset_schema: dict[str, Any],
        dataset_rows: int,
    ) -> KnowledgeDoc | None:
        async with self.factory() as db:
            doc = await db.get(KnowledgeDoc, doc_id)
            if doc is None:
                return None
            doc.dataset_path = dataset_path[:512]
            doc.dataset_schema = dataset_schema
            doc.dataset_rows = dataset_rows
            doc.status = "ready"
            doc.error = ""
            kb = await db.get(KnowledgeBase, doc.kb_id)
            if kb is not None:
                kb.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(doc)
            return doc

    async def set_doc_status(self, doc_id: str, status: str, error: str = "") -> None:
        async with self.factory() as db:
            doc = await db.get(KnowledgeDoc, doc_id)
            if doc is None:
                return
            doc.status = status
            doc.error = (error or "")[:2000]
            await db.commit()

    async def docs_to_recover(self) -> list[KnowledgeDoc]:
        """Docs left mid-ingest by a crash/restart (pending or processing)."""
        async with self.factory() as db:
            rows = await db.scalars(
                select(KnowledgeDoc).where(KnowledgeDoc.status.in_(("pending", "processing")))
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
    # Recall threshold for word_similarity. Kept at the original 0.12 so this
    # rewrite preserves what the agent used to retrieve — the change is purely
    # that the query now uses the pg_trgm OPERATOR form (`<%`, ILIKE), which the
    # GIN trigram index can serve, instead of the FUNCTION form (which forced a
    # sequential scan computing similarity for every chunk in scope).
    _WORD_SIM_THRESHOLD = 0.12
    # Cap query variants per search so the OR-expansion stays bounded.
    _MAX_QUERIES = 5

    async def search(self, query: str, kb_ids: list[str], limit: int = 6) -> list[dict[str, Any]]:
        """Top matching chunks across the given bases, best first (single query)."""
        return await self.search_multi([query], kb_ids, limit=limit)

    async def search_multi(
        self, queries: list[str], kb_ids: list[str], limit: int = 6
    ) -> list[dict[str, Any]]:
        """Top matching chunks for ANY of several query phrasings (synonyms, the
        other language, keyword variants), scored by the BEST-matching variant.

        This is lexical multi-query expansion — recall approaching semantic search
        for paraphrased questions, at zero extra infrastructure: it's still one
        GIN-indexed SQL statement, just OR-ing the variants' pg_trgm predicates
        and taking GREATEST() of their word-similarities as the rank.
        """
        # Dedupe (case-insensitively), drop blanks, and cap the fan-out.
        seen: dict[str, str] = {}
        for q in queries:
            q = (q or "").strip()
            if q and q.lower() not in seen:
                seen[q.lower()] = q
        qs = list(seen.values())[: self._MAX_QUERIES]
        if not qs or not kb_ids:
            return []
        async with self.factory() as db:
            if self.is_postgres:
                # Transaction-local so it never leaks to other pooled connections.
                await db.execute(
                    sa_text(f"SET LOCAL pg_trgm.word_similarity_threshold = {self._WORD_SIM_THRESHOLD}")
                )
                sims = ", ".join(f"word_similarity(:q{i}, c.text)" for i in range(len(qs)))
                score_expr = f"GREATEST({sims})" if len(qs) > 1 else sims
                # Each variant contributes two GIN-servable predicates (`<%`, ILIKE),
                # OR-ed together, so the planner BitmapOrs index scans — no full scan.
                where_ors = " OR ".join(f"(:q{i} <% c.text OR c.text ILIKE :like{i})" for i in range(len(qs)))
                stmt = sa_text(
                    f"SELECT c.text, c.title, c.page, c.kb_id, b.name AS kb_name, "
                    f"{score_expr} AS score "
                    "FROM knowledge_chunks c JOIN knowledge_bases b ON b.id = c.kb_id "
                    "WHERE c.kb_id IN :ids "
                    f"AND ({where_ors}) "
                    "ORDER BY score DESC LIMIT :limit"
                ).bindparams(bindparam("ids", expanding=True))
                params: dict[str, Any] = {"ids": kb_ids, "limit": limit}
                for i, q in enumerate(qs):
                    params[f"q{i}"] = q
                    params[f"like{i}"] = f"%{q}%"
                rows = (await db.execute(stmt, params)).all()
                return [
                    {
                        "text": r[0],
                        "title": r[1],
                        "page": r[2],
                        "kb_id": r[3],
                        "kb_name": r[4],
                        "score": float(r[5]),
                    }
                    for r in rows
                ]
            # SQLite fallback: substring match on any variant.
            clauses = [KnowledgeChunk.text.ilike(f"%{q}%") for q in qs]
            rows = (
                await db.execute(
                    select(
                        KnowledgeChunk.text,
                        KnowledgeChunk.title,
                        KnowledgeChunk.page,
                        KnowledgeChunk.kb_id,
                    )
                    .where(KnowledgeChunk.kb_id.in_(kb_ids), or_(*clauses))
                    .limit(limit)
                )
            ).all()
            return [
                {"text": r[0], "title": r[1], "page": r[2], "kb_id": r[3], "kb_name": "", "score": 1.0}
                for r in rows
            ]


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Timestamps are stored as DateTime(timezone=True), but SQLite has no tz
    type and hands back naive values — comparing those against an aware now()
    raises. Everything written here is UTC, so attach that rather than crash."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
            share = (await db.scalars(select(Share).where(Share.token_hash == _hash_token(token)))).first()
            if share is None or share.revoked:
                return None
            if share.expires_at is not None and _as_utc(share.expires_at) < datetime.now(timezone.utc):
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
