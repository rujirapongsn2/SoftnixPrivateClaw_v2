"""Data access: append-only message store, memory store, users, audit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    and_,
    bindparam,
    case,
    cast,
    exists,
    false as sa_false,
    func,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from sbot.core.keyed_locks import KeyedLocks
from sbot.core.plans import cost_allowed, cost_rank
from sbot.db.models import (
    AppSetting,
    AuditEvent,
    Blueprint,
    BlueprintVersion,
    Bot,
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
    Mission,
    MissionBlackboard,
    MissionNode,
    PolicyPlan,
    Schedule,
    Share,
    Skill,
    UsageDaily,
    UsageRecord,
    User,
    UserGroup,
    _uuid,
)


def _day_key(bucket_value: Any) -> str:
    """Normalize a date_trunc (datetime, Postgres) or strftime (str, SQLite)
    day bucket into a plain "YYYY-MM-DD" key."""
    return bucket_value.date().isoformat() if hasattr(bucket_value, "date") else str(bucket_value)


def _is_transient_tool_narration(tool_calls: Any) -> bool:
    """Whether assistant text attached to tool calls is only progress narration.

    A delegation is the exception: its leader's short introduction gives the
    specialist reply that follows a human-readable origin. All other tool-call
    text is transient progress already represented in the Execution panel.
    """
    if not tool_calls:
        return False
    if not isinstance(tool_calls, list):
        return True
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict) and function.get("name") in {"delegate", "delegate_many"}:
            return False
    return True


class MessageStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres
        # `seq` is derived from a SELECT and committed on a later await, so two
        # writers to one session can read the same maximum and both use it.
        # Turns are already serialized per session by the runtime, but not every
        # writer goes through a turn — a policy-blocked message is stored outside
        # the lock, and a delegation mirrors into the specialist's own thread —
        # and a duplicate seq is unrecoverable: `page_for_display` walks
        # backwards on a strictly-less-than cursor, so the twin below the page
        # boundary can never be asked for again.
        self._seq_locks = KeyedLocks()

    async def append(self, session_id: str, entries: list[dict[str, Any]], *, delivery_key: str | None = None) -> int:
        """Append turn messages atomically with monotonic per-session seq.

        Returns the seq of the first entry written (0 for an empty append), so a
        caller that has to enrich a message it already made durable can address
        it via set_content().
        """
        if not entries:
            return 0
        if delivery_key and len(entries) != 1:
            raise ValueError("A durable delivery must contain exactly one message")
        async with self._seq_locks.get(session_id), self.factory() as db:
            if delivery_key and await db.get(Message, delivery_key) is not None:
                return 0
            next_seq = (
                await db.scalar(
                    select(func.coalesce(func.max(Message.seq), 0)).where(Message.session_id == session_id)
                )
            ) + 1
            for offset, entry in enumerate(entries):
                db.add(
                    Message(
                        **({"id": delivery_key} if delivery_key else {}),
                        session_id=session_id,
                        seq=next_seq + offset,
                        role=entry["role"],
                        content=entry.get("content") or "",
                        tool_calls=entry.get("tool_calls"),
                        tool_call_id=entry.get("tool_call_id"),
                        tool_name=entry.get("name"),
                        meta=entry.get("meta"),
                        speaker_bot_id=entry.get("speaker_bot_id"),
                    )
                )
            session = await db.get(ChatSession, session_id)
            if session is not None:
                session.updated_at = datetime.now(timezone.utc)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                if delivery_key and await db.get(Message, delivery_key) is not None:
                    return 0
                raise
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
        """Load recent messages in chronological order, in LLM message format.

        A message with a `speaker_bot_id` is another bot's reply, kept so the
        transcript can show who said what. It is left out here: the delegating
        model already received that same text as the `delegate` tool's result,
        so replaying it would spend the context window twice on one answer and
        put words in the assistant's own mouth that it never said.
        """
        async with self.factory() as db:
            rows = (
                await db.scalars(
                    select(Message)
                    .where(
                        Message.session_id == session_id,
                        Message.seq > after_seq,
                        Message.speaker_bot_id.is_(None),
                    )
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
                if row.meta.get("delegated_by"):
                    # Stored as a `user` message because that is the position it
                    # occupies in the bot's own thread, but a leader wrote it,
                    # not the human. Unmarked, the bot's next direct turn reads
                    # an assignment it was handed as something the user said —
                    # and memory consolidation renders the same rows, so it ends
                    # up in the bot's long-term doc that way too.
                    entry["content"] = f"[Assigned by your team lead]\n{row.content}"
            out.append(entry)
        return out

    async def page_for_display(
        self, session_id: str, *, before_seq: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], bool]:
        """One page of a session's human-visible transcript, oldest-first.

        Reading walks backwards from the newest message, so `before_seq` is the
        lowest `seq` the caller already holds; it is exclusive. Also returns
        whether older messages remain.

        Separate from `recent()` rather than a flag on it: that one builds the
        turn's messages for a provider, where the `seq` a pager needs is an
        unrecognized key and a 400.

        Tool traffic and transient assistant narration attached to a tool call
        are excluded here rather than by the caller. The latter is status text
        (for example, "I will check that first") and becomes a confusing
        permanent chat bubble after reload. Delegation narration stays visible
        so a specialist's reply retains its origin. All rows remain in the
        database and in `recent()` so the model retains tool-call/result pairs.

        `tool_calls` is a JSON column and its SQL null semantics differ between
        SQLite and Postgres, so filtering happens in Python. Fetch raw chunks
        until we have one extra *visible* row; that keeps pagination exact even
        when a turn produced many tool calls.
        """
        window = [
            Message.session_id == session_id,
            Message.role.in_(("user", "assistant")),
        ]
        visible: list[Message] = []
        cursor = before_seq
        # This is an internal chunk, not a user-visible page size. A generous
        # minimum avoids N+1 queries for ordinary tool-heavy turns while still
        # keeping a pathological all-tool transcript bounded in memory.
        chunk_size = max(limit + 1, 128)
        while len(visible) <= limit:
            conditions = [*window]
            if cursor is not None:
                conditions.append(Message.seq < cursor)
            async with self.factory() as db:
                rows = (
                    await db.scalars(
                        select(Message).where(*conditions).order_by(Message.seq.desc()).limit(chunk_size)
                    )
                ).all()
            if not rows:
                break
            visible.extend(
                row for row in rows
                if not (row.role == "assistant" and _is_transient_tool_narration(row.tool_calls))
            )
            cursor = rows[-1].seq
            if len(rows) < chunk_size:
                break
        # Reading one visible row past the page is what makes `has_more` exact.
        has_more = len(visible) > limit
        return [
            {
                "seq": r.seq,
                "role": r.role,
                "content": r.content,
                "meta": r.meta,
                "speaker_bot_id": r.speaker_bot_id,
            }
            # Descending, so the surplus row is the oldest — trim before
            # reversing or the page slides one message away from the cursor.
            for r in reversed(visible[:limit])
        ], has_more

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


class BotStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory
        # A roster must be checked and written as one critical section.  The
        # database constraint below is the cross-process backstop; this lock
        # keeps concurrent turns in this worker from needlessly racing first.
        self._create_locks = KeyedLocks()

    async def create_batch(
        self,
        owner_id: str,
        bots: Sequence[dict[str, Any]],
        *,
        max_bots: int,
    ) -> list[Bot]:
        """Atomically add a validated roster for one owner.

        The caller validates generated fields such as the charter and tool
        allowlist.  This method repeats the quota/name checks inside the
        transaction because those facts can change between tool validation and
        the write.  It intentionally leaves an archived bot's name reusable.
        """
        if not bots:
            return []
        async with self._create_locks.get(owner_id):
            async with self.factory() as db:
                # Lock the owner row before counting.  KeyedLocks covers this
                # process; this row lock makes quota validation serialize
                # across web workers that share the same PostgreSQL database.
                owner = await db.scalar(select(User.id).where(User.id == owner_id).with_for_update())
                if owner is None:
                    raise ValueError("team owner no longer exists")
                active = list(
                    await db.scalars(
                        select(Bot).where(Bot.owner_id == owner_id, Bot.is_archived.is_(False))
                    )
                )
                if len(active) + len(bots) > max_bots:
                    raise ValueError(
                        f"creating {len(bots)} bots would exceed the maximum of {max_bots} bots for this team"
                    )

                existing_names = {bot.name.casefold() for bot in active}
                requested_names: set[str] = set()
                for item in bots:
                    name = str(item["name"]).strip()
                    key = name.casefold()
                    if key in existing_names:
                        raise ValueError(f"bot with name '{name}' already exists")
                    if key in requested_names:
                        raise ValueError(f"duplicate bot name '{name}' in this request")
                    requested_names.add(key)

                created = [
                    Bot(
                        owner_id=owner_id,
                        name=str(item["name"]).strip(),
                        role_title=str(item.get("role_title") or "Specialist"),
                        charter=str(item.get("charter") or ""),
                        model=item.get("model"),
                        tool_allowlist=item.get("tool_allowlist"),
                        skill_ids=item.get("skill_ids"),
                        kind=str(item.get("kind") or "specialist"),
                        avatar=item.get("avatar")
                        or {"color": "#4b6bfb", "emoji": "🤖", "initial": str(item["name"]).strip()[:1].upper()},
                        created_by=str(item.get("created_by") or "user"),
                    )
                    for item in bots
                ]
                db.add_all(created)
                try:
                    # Flush first so a constraint error rolls back the whole
                    # roster before anything can be observed as created.
                    await db.flush()
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
                return created

    async def create(
        self,
        owner_id: str,
        name: str,
        role_title: str = "Specialist",
        charter: str = "",
        model: str | None = None,
        tool_allowlist: list[str] | None = None,
        skill_ids: list[str] | None = None,
        kind: str = "specialist",
        avatar: dict | None = None,
        created_by: str = "user",
    ) -> Bot:
        async with self.factory() as db:
            bot = Bot(
                owner_id=owner_id,
                name=name,
                role_title=role_title,
                charter=charter,
                model=model,
                tool_allowlist=tool_allowlist,
                skill_ids=skill_ids,
                kind=kind,
                avatar=avatar or {"color": "#4b6bfb", "emoji": "🤖", "initial": name[:1].upper()},
                created_by=created_by,
            )
            db.add(bot)
            await db.commit()
            return bot

    async def get(self, bot_id: str, owner_id: str) -> Bot | None:
        """Owner-scoped lookup. `owner_id` is required rather than optional
        because the id often originates from an LLM tool call, where a missing
        filter is a cross-tenant read.

        Archived bots are excluded, like every other read here: deletion is
        soft, and sessions keep their `bot_id` forever, so a lookup that
        returned them let a bot the user had deleted go on running turns with
        its charter, its tool allowlist and its private memory — invisible in
        the bot list and 404 from the API while still answering in chat. Use
        `list_for_user(include_archived=True)` when the archive itself is what
        you want."""
        async with self.factory() as db:
            return await db.scalar(
                select(Bot).where(
                    Bot.id == bot_id,
                    Bot.owner_id == owner_id,
                    Bot.is_archived.is_(False),
                )
            )

    async def get_by_name(self, owner_id: str, name: str) -> Bot | None:
        async with self.factory() as db:
            return await db.scalar(
                select(Bot).where(Bot.owner_id == owner_id, Bot.name == name, Bot.is_archived.is_(False))
            )

    async def list_for_user(
        self, owner_id: str, include_archived: bool = False, limit: int = 200
    ) -> list[Bot]:
        async with self.factory() as db:
            query = select(Bot).where(Bot.owner_id == owner_id)
            if not include_archived:
                query = query.where(Bot.is_archived.is_(False))
            query = query.order_by(Bot.created_at.asc()).limit(min(limit, 500))
            rows = await db.scalars(query)
            return list(rows)

    async def count_for_user(self, owner_id: str) -> int:
        async with self.factory() as db:
            return int(
                await db.scalar(
                    select(func.count())
                    .select_from(Bot)
                    .where(Bot.owner_id == owner_id, Bot.is_archived.is_(False))
                )
                or 0
            )

    async def get_or_create_cos(self, owner_id: str) -> Bot:
        """Ensure Chief of Staff exists for this owner."""
        async with self.factory() as db:
            cos = await db.scalar(
                select(Bot).where(
                    Bot.owner_id == owner_id,
                    Bot.kind == "chief_of_staff",
                    Bot.is_archived.is_(False),
                )
            )
            if cos is not None:
                return cos

            cos = Bot(
                owner_id=owner_id,
                name="บุ้ย",
                role_title="Chief of Staff",
                charter=(
                    "คุณคือ 'บุ้ย' Chief of Staff หัวหน้าทีม AI อัจฉริยะ "
                    "มีหน้าที่รับโจทย์จากผู้ใช้ วิเคราะห์ วางแผน แตกงาน มอบหมายงานให้บอทผู้เชี่ยวชาญ "
                    "และติดตามผลมารายงานผู้ใช้อย่างกระชับและชัดเจน"
                ),
                kind="chief_of_staff",
                avatar={"color": "#0ea5e9", "emoji": "👑", "initial": "บ"},
                created_by="system",
            )
            db.add(cos)
            await db.commit()
            return cos

    async def update(self, bot_id: str, owner_id: str, **fields: Any) -> Bot | None:
        async with self.factory() as db:
            bot = await db.scalar(select(Bot).where(Bot.id == bot_id, Bot.owner_id == owner_id))
            if bot is None:
                return None
            for k, v in fields.items():
                if hasattr(bot, k):
                    setattr(bot, k, v)
            await db.commit()
            return bot

    async def archive(self, bot_id: str, owner_id: str) -> bool:
        async with self.factory() as db:
            bot = await db.scalar(select(Bot).where(Bot.id == bot_id, Bot.owner_id == owner_id))
            if bot is None:
                return False
            bot.is_archived = True
            await db.commit()
            return True


# A mission graph large enough to exceed this is a planning bug, not a mission.
# The cap is enforced on write so `get_nodes` (called once per scheduler pass)
# can stay a single unbounded SELECT without becoming a scaling hazard.
MAX_NODES_PER_MISSION = 500

# Widths of the MissionNode columns a mission plan fills in. The plan is written
# by an LLM, so these are untrusted input: on Postgres an over-long value raises
# DataError, which is not InvalidGraphError — MissionService.plan()'s cleanup
# never runs, the half-built mission is left looking `planned`, and the caller
# gets a 500 instead of a 400. SQLite enforces no width at all, so the same plan
# succeeds in tests and fails in production. Checked here rather than left to the
# database for both reasons.
_MAX_NODE_ID_CHARS = 32
_MAX_NODE_KIND_CHARS = 32
_MAX_NODE_TITLE_CHARS = 255
# Statuses a node can be claimed *from*. `running` is deliberately absent: that
# is what makes double execution impossible even when two workers race.
_CLAIMABLE_STATUSES = ("pending", "ready", "error")
_MANIFEST_PREVIEW_CHARS = 120


class MissionStore:
    """Mission graph persistence.

    Two access tiers on purpose: `get_mission`/`list_missions` take an
    `owner_id` and are what API handlers and bot-facing tools must use, while
    `*_unchecked` exists only for the scheduler, which operates on a mission id
    it was handed by an already-authorized caller. The ugly name is the point —
    an unchecked read should be visible in review.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    # ---------------------------------------------------------------- mission
    async def create_mission(
        self,
        owner_id: str,
        goal: str,
        session_id: str | None = None,
        budget: dict | None = None,
        status: str = "running",
        mission_id: str | None = None,
    ) -> Mission:
        async with self.factory() as db:
            mission = Mission(
                owner_id=owner_id,
                goal=goal,
                session_id=session_id,
                budget=budget,
                spent={},
                status=status,
            )
            if mission_id is not None:
                mission.id = mission_id
            db.add(mission)
            await db.commit()
            return mission

    async def get_mission(self, mission_id: str, owner_id: str) -> Mission | None:
        async with self.factory() as db:
            return await db.scalar(
                select(Mission).where(Mission.id == mission_id, Mission.owner_id == owner_id)
            )

    async def get_mission_unchecked(self, mission_id: str) -> Mission | None:
        """No owner filter — scheduler only. Callers reachable from a request or
        from a model-supplied id must use `get_mission`."""
        async with self.factory() as db:
            return await db.get(Mission, mission_id)

    async def pending_counts(self, owner_id: str) -> dict[str, int]:
        async with self.factory() as db:
            pending = Mission.status.in_(['queued', 'running', 'blocked', 'paused'])
            total = await db.scalar(select(func.count()).select_from(Mission).where(pending))
            own = await db.scalar(select(func.count()).select_from(Mission).where(pending, Mission.owner_id == owner_id))
            return {'owner': own or 0, 'total': total or 0}

    async def begin_queued(self, mission_id: str) -> bool:
        async with self.factory() as db:
            result = await db.execute(update(Mission).where(
                Mission.id == mission_id, Mission.status == 'queued'
            ).values(status='running'))
            await db.commit()
            return result.rowcount == 1

    async def reportable_missions(self, offset: int = 0, limit: int = 100) -> list[Mission]:
        """Scheduler-only scan; committed outcomes are the durable delivery queue."""
        async with self.factory() as db:
            return list(await db.scalars(select(Mission).where(
                Mission.status.in_(["completed", "failed", "blocked", "paused"]),
                Mission.session_id.is_not(None),
            ).order_by(Mission.id).offset(offset).limit(limit)))

    async def list_missions(
        self, owner_id: str, status: str | None = None, limit: int = 50, session_id: str | None = None
    ) -> list[Mission]:
        async with self.factory() as db:
            query = select(Mission).where(Mission.owner_id == owner_id)
            if session_id is not None:
                query = query.where(Mission.session_id == session_id)
            if status:
                query = query.where(Mission.status == status)
            query = query.order_by(Mission.created_at.desc()).limit(min(limit, 200))
            return list(await db.scalars(query))

    async def active_missions(
        self, owner_id: str, limit: int = 20, session_id: str | None = None
    ) -> list[tuple[Mission, list[MissionNode]]]:
        """A user's in-flight missions together with their nodes, newest first.

        `blocked` counts as active alongside `running`. It reads like a finished
        state but it is the opposite: the mission is parked on a gate and cannot
        move until the user decides, so leaving it out would hide the one case
        they actually have to act on.

        Two statements rather than a join: a join repeats every mission column
        once per node, and the caller wants them grouped by mission anyway.
        """
        async with self.factory() as db:
            missions = list(
                await db.scalars(
                    select(Mission)
                    .where(
                        Mission.owner_id == owner_id,
                        Mission.status.in_(("queued", "running", "blocked", "paused")),
                        *([Mission.session_id == session_id] if session_id is not None else []),
                    )
                    .order_by(Mission.created_at.desc())
                    .limit(max(1, min(limit, 100)))
                )
            )
            if not missions:
                return []
            grouped: dict[str, list[MissionNode]] = {m.id: [] for m in missions}
            nodes = await db.scalars(
                select(MissionNode)
                .where(MissionNode.mission_id.in_(list(grouped)))
                .order_by(MissionNode.id.asc())
            )
            for node in nodes:
                grouped[node.mission_id].append(node)
            return [(m, grouped[m.id]) for m in missions]

    async def interrupted_missions(self, limit: int = 20, offset: int = 0) -> list[Mission]:
        """Missions a dead process left mid-flight, across all owners.

        A `blocked` mission is only included when one of its nodes is stuck
        `running` under an expired lease — the process that claimed it died
        before the lease ran out, and nothing else ever revisits a `blocked`
        mission afterwards, so this is the only sweep that can reclaim and
        fail a genuinely orphaned node instead of leaving it stuck forever.

        A `blocked` mission with no such node is left alone on purpose: it is
        parked on a real, still-open gate, and nothing about it has changed.
        Resuming it would re-drive the engine to the exact same `blocked`
        outcome and re-fire `MissionService._report` for no reason, spamming
        the user with the same "needs your review" message on every restart
        for as long as the gate stays open.

        Unscoped on purpose: this feeds the startup sweep, which has no user to
        scope to — see `MissionService.resume_interrupted`. Bounded and
        oldest-first because the table grows forever, so a backlog drains in the
        order it was abandoned instead of the same tail being starved on every
        restart.
        """
        orphaned_node = exists(
            select(MissionNode.mission_id).where(
                MissionNode.mission_id == Mission.id,
                MissionNode.status == "running",
                MissionNode.lease_expires_at.is_not(None),
                MissionNode.lease_expires_at < datetime.now(timezone.utc),
            )
        )
        async with self.factory() as db:
            return list(
                await db.scalars(
                    select(Mission)
                    .where(
                        or_(
                            Mission.status.in_(("queued", "running")),
                            and_(Mission.status == "blocked", orphaned_node),
                        )
                    )
                    .order_by(Mission.created_at.asc())
                    .offset(offset)
                    .limit(max(1, min(limit, 200)))
                )
            )

    async def update_mission(self, mission_id: str, **fields: Any) -> Mission | None:
        """Write mission columns. Raises on a field that is not one.

        Every caller here is the scheduler moving a mission's lifecycle, and the
        most common field is `status`. A misspelled one used to be dropped in
        silence, which is the worst possible outcome for this particular write:
        the mission keeps its old status, so a failed or cancelled mission still
        looks `running` and the resume sweep picks it up forever. Validated
        against the mapped columns rather than `hasattr`, because a model also
        answers to names like `metadata` that are not columns at all.
        """
        unknown = sorted(set(fields) - set(Mission.__table__.columns.keys()))
        if unknown:
            raise ValueError(f"Mission has no column(s) {unknown}")
        async with self.factory() as db:
            mission = await db.get(Mission, mission_id)
            if mission is None:
                return None
            for key, value in fields.items():
                setattr(mission, key, value)
            await db.commit()
            return mission

    # ------------------------------------------------------------------ nodes
    async def add_nodes(self, mission_id: str, nodes: list[dict]) -> list[MissionNode]:
        """Persist nodes, rejecting any graph that could not execute.

        Validation runs over the union of existing and incoming nodes, so a
        replan that appends nodes cannot introduce a cycle or an edge to a node
        that does not exist.
        """
        # Local import: mission_engine imports this module, so a top-level
        # import here would be circular.
        from sbot.core.mission_engine import InvalidGraphError, MissionEngine

        async with self.factory() as db:
            existing = list(
                await db.scalars(select(MissionNode).where(MissionNode.mission_id == mission_id))
            )
            existing_ids = {n.id for n in existing}

            incoming: list[dict] = []
            for n in nodes:
                node_id = str(n.get("id") or _uuid())
                # Rejected rather than clamped: other nodes reference this id in
                # their `depends_on`, so truncating it would rewire the graph
                # behind validate_dag's back.
                if len(node_id) > _MAX_NODE_ID_CHARS:
                    raise InvalidGraphError(
                        f"node id {node_id[:40]!r} exceeds {_MAX_NODE_ID_CHARS} characters"
                    )
                kind = str(n.get("kind") or "task")
                if len(kind) > _MAX_NODE_KIND_CHARS:
                    raise InvalidGraphError(f"node {node_id!r} has an unusable kind {kind[:40]!r}")
                if node_id in existing_ids:
                    raise InvalidGraphError(f"node {node_id!r} already exists in this mission")
                existing_ids.add(node_id)
                required_files = n.get('required_files', [])
                if not isinstance(required_files, list) or len(required_files) > 50 or any(
                    not isinstance(path, str) or not path.strip() for path in required_files
                ):
                    raise InvalidGraphError('required_files must be a list of at most 50 nonempty paths')
                incoming.append({**n, "id": node_id, "kind": kind})

            if len(existing) + len(incoming) > MAX_NODES_PER_MISSION:
                raise InvalidGraphError(
                    f"mission would exceed {MAX_NODES_PER_MISSION} nodes "
                    f"({len(existing)} existing + {len(incoming)} new)"
                )

            MissionEngine.validate_dag(
                [{"id": n.id, "depends_on": n.depends_on or []} for n in existing] + incoming
            )

            created = []
            for n in incoming:
                node = MissionNode(
                    id=n["id"],
                    mission_id=mission_id,
                    bot_id=n.get("bot_id"),
                    kind=n["kind"],
                    # Clamped, not rejected: a title is what the step is called,
                    # nothing references it, and losing an over-long tail is a
                    # better outcome than refusing an otherwise valid plan.
                    title=str(n.get("title") or "")[:_MAX_NODE_TITLE_CHARS],
                    instruction=n.get("instruction", ""),
                    depends_on=n.get("depends_on") or [],
                    status=n.get("status", "pending"),
                    max_attempts=n.get("max_attempts", 3),
                    budget={**(n.get("budget") or {}), 'required_files': n.get('required_files', [])},
                )
                db.add(node)
                created.append(node)
            await db.commit()
            return created

    async def get_nodes(self, mission_id: str) -> list[MissionNode]:
        async with self.factory() as db:
            return list(
                await db.scalars(
                    select(MissionNode)
                    .where(MissionNode.mission_id == mission_id)
                    .order_by(MissionNode.id.asc())
                    .limit(MAX_NODES_PER_MISSION)
                )
            )

    async def claim_node(
        self, mission_id: str, node_id: str, worker_id: str, lease_seconds: float
    ) -> MissionNode | None:
        """Take exclusive ownership of a node, or return None if someone else did.

        This is the single point that authorizes execution. The conditional
        UPDATE flips the node to `running` and bumps `attempts` in one statement,
        so two schedulers (or two passes of the same scheduler) racing on the
        same node produce exactly one winner and one None.
        """
        now = datetime.now(timezone.utc)
        async with self.factory() as db:
            result = await db.execute(
                update(MissionNode)
                .where(
                    MissionNode.mission_id == mission_id,
                    MissionNode.id == node_id,
                    MissionNode.status.in_(_CLAIMABLE_STATUSES),
                    MissionNode.attempts < MissionNode.max_attempts,
                )
                .values(
                    status="running",
                    attempts=MissionNode.attempts + 1,
                    lease_owner=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=func.coalesce(MissionNode.started_at, now),
                    finished_at=None,
                )
            )
            if result.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
            return await db.get(MissionNode, {"mission_id": mission_id, "id": node_id})

    async def finish_node(
        self,
        mission_id: str,
        node_id: str,
        status: str,
        output: str | None = None,
        artifacts: list[str] | None = None,
        cost: dict[str, float] | None = None,
        lease_owner: str | None = None,
        attempt: int | None = None,
        task_result: dict | None = None,
    ) -> None:
        """Record a node's outcome and release its lease."""
        values: dict[str, Any] = {
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "finished_at": datetime.now(timezone.utc),
        }
        if output is not None:
            values["output"] = output
        if artifacts:
            values["artifacts"] = artifacts
        if cost:
            values["cost"] = cost
        conditions = [MissionNode.mission_id == mission_id, MissionNode.id == node_id]
        if lease_owner is not None:
            conditions += [MissionNode.status == "running", MissionNode.lease_owner == lease_owner,
                           MissionNode.attempts == attempt]
        async with self.factory() as db:
            changed = await db.execute(
                update(MissionNode)
                .where(*conditions)
                .values(**values)
            )
            if changed.rowcount:
                records = {}
                if output is not None:
                    records[f'result:{node_id}'] = output
                if task_result is not None:
                    records[f'contract:{node_id}'] = task_result
                for key, value in records.items():
                    record = await db.scalar(select(MissionBlackboard).where(
                        MissionBlackboard.mission_id == mission_id, MissionBlackboard.key == key))
                    if record is None:
                        db.add(MissionBlackboard(mission_id=mission_id, key=key, value=value,
                                                 written_by_node=node_id))
                    else:
                        record.value = value
                        record.written_by_node = node_id
                        record.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def resolve_gate(
        self, mission_id: str, node_id: str, *, status: str, output: str
    ) -> bool:
        """Record a human's decision on a parked gate. True if it was applied.

        Conditional on the node still being `awaiting_human` for the same reason
        `claim_node` and `revise_node` are conditional: the decision arrives from
        outside the scheduler, so a stale one (a second click, or an approval for
        a gate a replan already skipped) must not re-open a settled node or
        satisfy dependents twice.
        """
        async with self.factory() as db:
            result = await db.execute(
                update(MissionNode)
                .where(
                    MissionNode.mission_id == mission_id,
                    MissionNode.id == node_id,
                    MissionNode.status == "awaiting_human",
                )
                .values(
                    status=status,
                    output=output,
                    lease_owner=None,
                    lease_expires_at=None,
                    # A declined gate stays parked, so it has not finished.
                    # Stamping it would claim the step settled, when the mission
                    # can still be resumed and must block at this gate again.
                    finished_at=(
                        None if status == "awaiting_human" else datetime.now(timezone.utc)
                    ),
                )
            )
            if not result.rowcount:
                await db.rollback()
                return False
            await db.commit()
            return True

    async def revise_node(
        self,
        mission_id: str,
        node_id: str,
        *,
        instruction: str | None = None,
        bot_id: str | None = None,
        extra_attempts: int = 1,
    ) -> MissionNode | None:
        """Re-arm a failed node with a revised instruction, for a replan.

        Returns the node, or None if it was not re-armed. Conditional on the row
        still being `error` and unleased for the same reason `claim_node` is
        conditional: re-arming a node another worker is running would make it
        legitimately claimable while its first copy is still executing, and the
        mission would pay for the same step twice.

        `max_attempts` is raised relative to the attempts already spent, so the
        revision gets a fresh allowance without inheriting an inflated one from
        a previous revision — the engine's replan cap is what bounds the total.

        `mission_id` is part of the key, and matching on it here is also the
        backstop for `node_id` arriving from an LLM-authored replan patch: a
        node named in one mission's patch cannot re-arm another mission's step.
        """
        extra_attempts = max(1, int(extra_attempts))
        values: dict[str, Any] = {
            "status": "pending",
            "max_attempts": MissionNode.attempts + extra_attempts,
            "finished_at": None,
        }
        if instruction is not None:
            values["instruction"] = instruction
        if bot_id is not None:
            values["bot_id"] = bot_id
        async with self.factory() as db:
            result = await db.execute(
                update(MissionNode)
                .where(
                    MissionNode.mission_id == mission_id,
                    MissionNode.id == node_id,
                    MissionNode.status == "error",
                    MissionNode.lease_owner.is_(None),
                )
                .values(**values)
            )
            if not result.rowcount:
                await db.rollback()
                return None
            await db.commit()
            return await db.get(MissionNode, {"mission_id": mission_id, "id": node_id})

    async def renew_node_lease(self, mission_id: str, node_id: str, worker_id: str,
                               attempt: int, lease_seconds: float) -> bool:
        async with self.factory() as db:
            result = await db.execute(
                update(MissionNode).where(
                    MissionNode.mission_id == mission_id, MissionNode.id == node_id,
                    MissionNode.status == "running", MissionNode.lease_owner == worker_id,
                    MissionNode.attempts == attempt,
                ).values(lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))
            )
            await db.commit()
            return result.rowcount == 1

    async def reclaim_expired_leases(self, mission_id: str) -> int:
        """Free nodes whose worker died mid-flight.

        They come back as `error` rather than `pending` so the persisted
        `attempts` still bounds them — a node that reliably kills its worker
        fails the mission instead of looping forever.
        """
        async with self.factory() as db:
            result = await db.execute(
                update(MissionNode)
                .where(
                    MissionNode.mission_id == mission_id,
                    MissionNode.status == "running",
                    MissionNode.lease_expires_at.is_not(None),
                    MissionNode.lease_expires_at < datetime.now(timezone.utc),
                )
                .values(
                    status="error",
                    output="Lease expired: the worker running this node stopped reporting.",
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await db.commit()
            return result.rowcount or 0

    # ------------------------------------------------------------- Blackboard
    async def blackboard_write(
        self, mission_id: str, key: str, value: Any, node_id: str | None = None
    ) -> None:
        async with self.factory() as db:
            row = await db.scalar(
                select(MissionBlackboard).where(
                    MissionBlackboard.mission_id == mission_id,
                    MissionBlackboard.key == key,
                )
            )
            if row is None:
                row = MissionBlackboard(
                    mission_id=mission_id,
                    key=key,
                    value=value,
                    written_by_node=node_id,
                )
                db.add(row)
            else:
                row.value = value
                row.written_by_node = node_id
                row.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def blackboard_read(self, mission_id: str, key: str) -> Any:
        async with self.factory() as db:
            row = await db.scalar(
                select(MissionBlackboard).where(
                    MissionBlackboard.mission_id == mission_id,
                    MissionBlackboard.key == key,
                )
            )
            return row.value if row else None

    async def blackboard_manifest(self, mission_id: str) -> dict[str, dict[str, Any]]:
        """Describe what a node could read without handing it the payloads.

        Every downstream node would otherwise carry the full blackboard in its
        prompt, and the mission's token cost would grow quadratically with the
        graph (PRD §3.5). Nodes get a preview plus the size, and call
        `blackboard_read` for the keys they actually need.
        """
        async with self.factory() as db:
            rows = await db.scalars(
                select(MissionBlackboard).where(MissionBlackboard.mission_id == mission_id)
            )
            manifest: dict[str, dict[str, Any]] = {}
            for r in rows:
                entry: dict[str, Any] = {
                    "type": type(r.value).__name__,
                    "written_by_node": r.written_by_node,
                }
                if isinstance(r.value, str):
                    entry["chars"] = len(r.value)
                    entry["preview"] = (
                        r.value
                        if len(r.value) <= _MANIFEST_PREVIEW_CHARS
                        else r.value[:_MANIFEST_PREVIEW_CHARS] + "…"
                    )
                elif isinstance(r.value, (int, float, bool)) or r.value is None:
                    entry["preview"] = r.value
                elif isinstance(r.value, (list, dict)):
                    entry["items"] = len(r.value)
                manifest[r.key] = entry
            return manifest


class SessionStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession], is_postgres: bool = True):
        self.factory = factory
        self.is_postgres = is_postgres
        # Serializes `thread_for_bot`'s get-or-create per (user, bot).
        self._thread_locks = KeyedLocks()

    async def create(
        self,
        user_id: str,
        title: str = "New chat",
        channel: str = "web",
        bot_id: str | None = None,
        group_id: str | None = None,
        kind: str = "direct",
    ) -> ChatSession:
        async with self.factory() as db:
            session = ChatSession(
                user_id=user_id,
                title=title,
                channel=channel,
                bot_id=bot_id,
                group_id=group_id,
                kind=kind,
            )
            db.add(session)
            await db.commit()
            return session

    async def get(self, session_id: str) -> ChatSession | None:
        async with self.factory() as db:
            return await db.get(ChatSession, session_id)

    async def thread_for_bot(self, user_id: str, bot_id: str, title: str) -> ChatSession:
        """The one thread that stands for this bot, created if it has none yet.

        Picks the most recently updated, which is the same thread the sidebar
        resolves a bot row to. Creating a second one instead would not give the
        bot a second view: the team list shows one row per bot, and the extra
        thread drops out of it into "Other".

        Held under a lock because the lookup and the insert are two awaits: a
        `delegate_many` naming the same bot twice had both calls find nothing
        and both create one. There is deliberately no unique constraint to lean
        on instead — a user may have as many ordinary chats with a bot as they
        like, and this is only asking for the one that stands for it.
        """
        async with self._thread_locks.get(f"{user_id}:{bot_id}"), self.factory() as db:
            existing = await db.scalar(
                select(ChatSession)
                .where(ChatSession.user_id == user_id, ChatSession.bot_id == bot_id)
                .order_by(ChatSession.updated_at.desc())
                .limit(1)
            )
            if existing is not None:
                return existing
            session = ChatSession(user_id=user_id, title=title, bot_id=bot_id)
            db.add(session)
            await db.commit()
            return session

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
        from sbot.db.models import BotChatGroup
        async with self.factory() as db:
            await db.execute(BotChatGroup.__table__.delete().where(BotChatGroup.session_id == session_id))
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

    @staticmethod
    def _core_query(user_id: str, scope: str, scope_id: str | None):
        """One living core doc per (user, scope, scope_id).

        `scope_id` is matched including its NULL case rather than skipped when
        absent: filtering on `scope` alone would let the shared user doc match a
        scoped row, so one bot's private memory could be served as — or
        overwritten by — the user's.
        """
        query = select(Memory).where(Memory.user_id == user_id, Memory.kind == "core", Memory.scope == scope)
        return query.where(Memory.scope_id.is_(None) if scope_id is None else Memory.scope_id == scope_id)

    async def get_core(self, user_id: str, scope: str = "user", scope_id: str | None = None) -> str:
        async with self.factory() as db:
            row = await db.scalar(self._core_query(user_id, scope, scope_id))
            return row.content if row else ""

    async def set_core(self, user_id: str, content: str, scope: str = "user", scope_id: str | None = None) -> None:
        async with self.factory() as db:
            row = await db.scalar(self._core_query(user_id, scope, scope_id))
            if row is None:
                db.add(Memory(user_id=user_id, scope=scope, scope_id=scope_id, kind="core", content=content))
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
                    "FROM sbot_memories "
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

    async def enabled_for_user(self, user_id: str) -> list[Skill]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(Skill).where(Skill.user_id == user_id, Skill.enabled.is_(True)).order_by(Skill.name)
            )
            return list(rows)

    async def get_by_name(self, user_id: str, name: str) -> Skill | None:
        async with self.factory() as db:
            return await db.scalar(select(Skill).where(Skill.user_id == user_id, Skill.name == name))

    async def upsert(self, user_id: str, name: str, **fields: Any) -> Skill:
        async with self.factory() as db:
            skill = await db.scalar(select(Skill).where(Skill.user_id == user_id, Skill.name == name))
            if skill is None:
                skill = Skill(user_id=user_id, name=name)
                db.add(skill)
            for key in ("description", "content", "enabled"):
                if key in fields and fields[key] is not None:
                    setattr(skill, key, fields[key])
            # Unlike the fields above, connector_id's presence itself is the
            # signal (None is a valid, meaningful value — "no connector
            # linked" — not "leave unchanged"), so callers must omit the key
            # entirely to leave it alone.
            if "connector_id" in fields:
                skill.connector_id = fields["connector_id"]
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

    async def set_project_policy(
        self, user_id: str, enabled: bool | None, limit: int | None
    ) -> User | None:
        """Set an account override, or clear both fields to inherit its group."""
        async with self.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return None
            user.project_containers_enabled = enabled
            user.project_container_limit = limit
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
    """User groups with inherited plan and project-container policy defaults."""

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

    async def set_project_policy(
        self, group_id: str, enabled: bool, limit: int
    ) -> UserGroup | None:
        """Set the group default for persistent project containers."""
        async with self.factory() as db:
            row = await db.get(UserGroup, group_id)
            if row is None:
                return None
            row.project_containers_enabled = enabled
            row.project_container_limit = limit
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
        metrics: dict[str, int] | None = None,
    ) -> None:
        """Record token spend. `count_turn=False` for background work (memory
        consolidation): the tokens are real and belong in the bill, but they are
        not a chat turn the user took. It is persisted on the raw row as well as
        applied to the rollup, because every turn figure has to agree — the
        quota reads UsageDaily.turns while the admin and per-user reports count
        usage_records, and a user seeing more turns billed than their own quota
        shows would be reporting a bug we wrote.

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
        """Today's highest-volume users (by chat turns), bounded to `limit`
        rows — feeds the admin Plans overview's "who's near their quota" list
        without an unbounded per-user scan."""
        today = datetime.now(timezone.utc).date()
        async with self.factory() as db:
            rows = (
                await db.execute(
                    select(
                        UsageDaily.user_id,
                        func.coalesce(func.sum(UsageDaily.turns), 0).label("turns"),
                        func.coalesce(func.sum(UsageDaily.images), 0).label("images"),
                    )
                    .where(UsageDaily.day == today)
                    .group_by(UsageDaily.user_id)
                    .order_by(func.sum(UsageDaily.turns).desc())
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
        """Today's summed chat turns + image generations for a user — the
        counters the daily plan quotas (messages_per_day / images_per_day) are
        checked against."""
        today = datetime.now(timezone.utc).date()
        async with self.factory() as db:
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(UsageDaily.turns), 0),
                        func.coalesce(func.sum(UsageDaily.images), 0),
                    ).where(UsageDaily.day == today, UsageDaily.user_id == user_id)
                )
            ).one()
        return {"turns": int(row[0] or 0), "images": int(row[1] or 0)}

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
            # The auto-selected default is an admin-global concept only; private
            # models are never the global default (owner_id=None gates it).
            elif owner_id is None and fields.get("is_default"):
                # Exactly one default across all global models.
                await db.execute(
                    LLMModel.__table__.update()
                    .where(
                        LLMModel.provider_id.in_(select(LLMProvider.id).where(LLMProvider.owner_id.is_(None)))
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
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row.model_id if row else None

    async def default_model_for(self, max_cost: str | None = None) -> str | None:
        """The chat model to fall back to for a user on a plan with ceiling
        ``max_cost``. Prefers the admin default when the plan allows it, else
        the most capable (highest-cost) allowed enabled global chat model, so a
        restricted user never lands on a model their plan forbids. None when the
        plan allows nothing (or nothing is configured)."""
        async with self.factory() as db:
            rows = (
                (
                    await db.execute(
                        select(LLMModel)
                        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
                        .where(
                            LLMModel.enabled.is_(True),
                            LLMProvider.enabled.is_(True),
                            LLMModel.kind == "chat",
                            LLMProvider.owner_id.is_(None),  # global models only
                        )
                    )
                )
                .scalars()
                .all()
            )
        allowed = [m for m in rows if cost_allowed(max_cost, m.cost)]
        if not allowed:
            return None
        default = next((m for m in allowed if m.is_default), None)
        if default is not None:
            return default.model_id
        # No allowed default → most capable allowed tier wins.
        return max(allowed, key=lambda m: cost_rank(m.cost)).model_id

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


class TelegramConfigStore:
    """Admin-configured Telegram bot token, so the integration is turned on
    self-service from the Admin console instead of an env var + server restart.
    The token is encrypted at rest (same scheme as OAuthAppStore/LLMConfigStore).

    ``get()`` returns None when nobody has ever saved a config through the admin
    UI — callers should fall back to the SBOT_TELEGRAM_BOT_TOKEN env var in that
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
        self, owner_id: str, name: str, description: str = "", visibility: str = "private"
    ) -> KnowledgeBase:
        async with self.factory() as db:
            kb = KnowledgeBase(
                owner_id=owner_id,
                name=name[:120],
                description=description,
                visibility=visibility if visibility in self._VISIBILITIES else "private",
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


class BlueprintStore:
    """Metadata and immutable version history for reusable document templates."""

    _VISIBILITIES = ("private", "group", "public")

    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    async def create(
        self,
        *,
        blueprint_id: str,
        owner_id: str,
        name: str,
        description: str,
        visibility: str,
        filename: str,
        mime: str,
        size: int,
        storage_path: str,
    ) -> Blueprint:
        async with self.factory() as db:
            row = Blueprint(
                id=blueprint_id,
                owner_id=owner_id,
                name=name[:120],
                description=description,
                visibility=visibility if visibility in self._VISIBILITIES else "private",
                current_version=1,
            )
            db.add(row)
            db.add(
                BlueprintVersion(
                    blueprint_id=blueprint_id,
                    version=1,
                    filename=filename[:255],
                    mime=mime[:120],
                    size=size,
                    storage_path=storage_path[:512],
                    created_by=owner_id,
                )
            )
            await db.commit()
            await db.refresh(row)
            return row

    async def get(self, blueprint_id: str) -> Blueprint | None:
        async with self.factory() as db:
            return await db.get(Blueprint, blueprint_id)

    async def get_version(self, blueprint_id: str, version: int | None = None) -> BlueprintVersion | None:
        async with self.factory() as db:
            if version is None:
                row = await db.get(Blueprint, blueprint_id)
                if row is None:
                    return None
                version = row.current_version
            return (
                await db.scalars(
                    select(BlueprintVersion).where(
                        BlueprintVersion.blueprint_id == blueprint_id,
                        BlueprintVersion.version == version,
                    )
                )
            ).first()

    async def list_versions(self, blueprint_id: str) -> list[BlueprintVersion]:
        async with self.factory() as db:
            rows = await db.scalars(
                select(BlueprintVersion)
                .where(BlueprintVersion.blueprint_id == blueprint_id)
                .order_by(BlueprintVersion.version.desc())
            )
            return list(rows)

    async def list_accessible(self, user_id: str) -> list[dict[str, Any]]:
        owner = aliased(User)
        async with self.factory() as db:
            viewer = await db.get(User, user_id)
            viewer_group = viewer.group_id if viewer is not None else None
            group_clause = sa_false()
            if viewer_group is not None:
                group_clause = (Blueprint.visibility == "group") & (owner.group_id == viewer_group)
            rows = (
                await db.execute(
                    select(Blueprint, BlueprintVersion, owner.display_name)
                    .join(owner, owner.id == Blueprint.owner_id)
                    .join(
                        BlueprintVersion,
                        (BlueprintVersion.blueprint_id == Blueprint.id)
                        & (BlueprintVersion.version == Blueprint.current_version),
                    )
                    .where(
                        (Blueprint.owner_id == user_id)
                        | (Blueprint.visibility == "public")
                        | group_clause
                    )
                    .order_by(Blueprint.updated_at.desc())
                )
            ).all()
        return [
            {
                "id": bp.id,
                "name": bp.name,
                "description": bp.description,
                "visibility": bp.visibility,
                "owner_id": bp.owner_id,
                "owner_name": owner_name or "",
                "is_owner": bp.owner_id == user_id,
                "current_version": bp.current_version,
                "filename": version.filename,
                "mime": version.mime,
                "size": version.size,
                "updated_at": bp.updated_at.isoformat(),
            }
            for bp, version, owner_name in rows
        ]

    async def update(self, blueprint_id: str, **fields: Any) -> Blueprint | None:
        async with self.factory() as db:
            row = await db.get(Blueprint, blueprint_id)
            if row is None:
                return None
            if fields.get("name"):
                row.name = str(fields["name"])[:120]
            if fields.get("description") is not None:
                row.description = str(fields["description"])
            if fields.get("visibility") in self._VISIBILITIES:
                row.visibility = str(fields["visibility"])
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return row

    async def add_version(
        self,
        *,
        blueprint_id: str,
        created_by: str,
        filename: str,
        mime: str,
        size: int,
        storage_path_for_version: Callable[[int], str],
    ) -> BlueprintVersion | None:
        async with self.factory() as db:
            row = (
                await db.scalars(
                    select(Blueprint).where(Blueprint.id == blueprint_id).with_for_update()
                )
            ).first()
            if row is None:
                return None
            version = int(
                await db.scalar(
                    select(func.coalesce(func.max(BlueprintVersion.version), 0)).where(
                        BlueprintVersion.blueprint_id == blueprint_id
                    )
                )
                or 0
            ) + 1
            item = BlueprintVersion(
                blueprint_id=blueprint_id,
                version=version,
                filename=filename[:255],
                mime=mime[:120],
                size=size,
                storage_path=storage_path_for_version(version)[:512],
                created_by=created_by,
            )
            db.add(item)
            row.current_version = version
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(item)
            return item

    async def activate_version(self, blueprint_id: str, version: int) -> Blueprint | None:
        async with self.factory() as db:
            row = await db.get(Blueprint, blueprint_id)
            exists_version = await db.scalar(
                select(func.count()).select_from(BlueprintVersion).where(
                    BlueprintVersion.blueprint_id == blueprint_id,
                    BlueprintVersion.version == version,
                )
            )
            if row is None or not exists_version:
                return None
            row.current_version = version
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
            return row

    async def discard_version(self, blueprint_id: str, version: int) -> None:
        """Remove an incompletely stored revision and restore the latest good one."""
        async with self.factory() as db:
            await db.execute(
                BlueprintVersion.__table__.delete().where(
                    BlueprintVersion.blueprint_id == blueprint_id,
                    BlueprintVersion.version == version,
                )
            )
            row = await db.get(Blueprint, blueprint_id)
            if row is not None:
                latest = await db.scalar(
                    select(func.max(BlueprintVersion.version)).where(
                        BlueprintVersion.blueprint_id == blueprint_id
                    )
                )
                row.current_version = int(latest or 1)
                row.updated_at = datetime.now(timezone.utc)
            await db.commit()

    async def delete(self, blueprint_id: str) -> None:
        async with self.factory() as db:
            await db.execute(
                BlueprintVersion.__table__.delete().where(BlueprintVersion.blueprint_id == blueprint_id)
            )
            row = await db.get(Blueprint, blueprint_id)
            if row is not None:
                await db.delete(row)
            await db.commit()


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
