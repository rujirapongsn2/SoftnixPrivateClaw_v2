"""SQLAlchemy models — one relational store for everything.

Messages are append-only; nothing ever rewrites a conversation file.
JSON columns use the portable JSON type (JSONB on Postgres via dialect).
"""

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Aliased so every column below is NUL-stripping by construction — see
# claw/db/types.py. Declared here rather than per-column because the columns at
# risk (tool output, extracted PDF text, model-generated titles) are spread
# across many tables, and a column added later would silently miss the guard.
from sbot.db.types import NulSafeJSON as JSON
from sbot.db.types import NulSafeString as String
from sbot.db.types import NulSafeText as Text


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


from claw.db.models import Base


from claw.db.models import User


from claw.db.models import UserGroup


from claw.db.models import PolicyPlan


class Bot(Base):
    """sbot Bot: autonomous role configuration with persona, skills, and tools."""

    __tablename__ = "sbot_bots"
    __table_args__ = (
        Index("ix_sbot_bots_owner_name", "owner_id", "name"),
        # A retired member may be recreated, but two active members with the
        # same visible name make delegation ambiguous.  The partial unique
        # index is also the cross-process guard for roster creation.
        Index(
            "uq_sbot_bots_owner_name_active",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("is_archived = false"),
            sqlite_where=text("is_archived = 0"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    role_title: Mapped[str] = mapped_column(String(64), default="Specialist")
    avatar: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"color": str, "emoji": str, "initial": str}
    charter: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_allowlist: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ["read_file", "exec", ...]
    skill_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="specialist")  # "chief_of_staff" | "specialist"
    created_by: Mapped[str] = mapped_column(String(64), default="user")  # "user" | "bot:<id>"
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"missions": int, "success_rate": float}
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ChatSession(Base):
    __tablename__ = "sbot_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    bot_id: Mapped[str | None] = mapped_column(ForeignKey("sbot_bots.id"), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="direct")  # direct | group | mission
    title: Mapped[str] = mapped_column(String(255), default="New chat")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    # Sticky per-chat model choice (litellm id). Null = use the configured default.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_consolidated_seq: Mapped[int] = mapped_column(Integer, default=0)
    # Consecutive times the summarizer returned something unusable for the
    # window starting at last_consolidated_seq. Persisted (not just in-memory)
    # so a window it can never process is eventually skipped rather than
    # retried on every turn forever — see claw/core/memory.py's
    # _MAX_POISON_ATTEMPTS. Reset to 0 by any successful pass.
    consolidation_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Working plan for the current task: {"goal": str, "steps": [{"step", "status"}]}.
    # Pinned into the system prompt every turn (never trimmed) so the agent keeps
    # the thread on long/autonomous runs even after early messages scroll out of
    # context. Maintained by the agent via the `update_plan` tool. Null = no plan.
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Monotonic counter bumped on every write to `plan`. A background mission can
    # settle minutes after the turn that launched it, by which point the agent may
    # have replaced the plan entirely; the mission records the revision it saw and
    # refuses to write back if it has moved.
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class BotChatGroup(Base):
    """A team of one owner's bots; unrelated to administrative UserGroup."""
    __tablename__ = "sbot_bot_chat_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sbot_sessions.id"), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    member_ids: Mapped[list] = mapped_column(JSON)
    leader_id: Mapped[str] = mapped_column(ForeignKey("sbot_bots.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Message(Base):
    __tablename__ = "sbot_messages"
    __table_args__ = (Index("ix_sbot_messages_session_seq", "session_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sbot_sessions.id"), index=True)
    speaker_bot_id: Mapped[str | None] = mapped_column(ForeignKey("sbot_bots.id"), nullable=True, index=True)
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per session
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Memory(Base):
    """Memory: kind='core' (one living doc per scope) and kind='history' entries.
    scope: 'bot' | 'user' | 'org' | 'mission'
    """

    __tablename__ = "sbot_memories"
    __table_args__ = (
        Index("ix_sbot_memories_user_kind", "user_id", "kind"),
        Index("ix_sbot_memories_scope", "scope", "scope_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16), default="user")  # bot|user|org|mission
    scope_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # core|history
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Mission(Base):
    """Mission: long-horizon DAG workflow across multiple bots."""

    __tablename__ = "sbot_missions"
    __table_args__ = (
        Index("ix_sbot_missions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sbot_sessions.id"), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft | running | paused | blocked | completed | failed | cancelled
    budget: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {max_tokens, max_wall_seconds, max_node_attempts, max_cost}
    spent: Mapped[dict | None] = mapped_column(JSON, default=dict)  # {tokens, seconds, cost}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0)
    # Which working-plan steps this mission is responsible for, captured at submit:
    # {"revision": int, "steps": [{"index": int, "text": str}]}. Plan steps have no
    # stable ids — the agent resends the whole list each `update_plan` call — so the
    # text is snapshotted alongside the index and both must still match at settle.
    plan_link: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class MissionNode(Base):
    """MissionNode: single task node in a mission DAG."""

    __tablename__ = "sbot_mission_nodes"
    __table_args__ = (
        Index("ix_sbot_mission_nodes_mission_status", "mission_id", "status"),
        Index("ix_sbot_mission_nodes_lease", "lease_expires_at"),
    )

    # Keyed by (id, mission_id), not id alone. Node ids come from an
    # LLM-authored plan and are semantic — "research", "write", "review" — so a
    # globally unique id meant the second mission to name a step the obvious way
    # failed on INSERT with an IntegrityError that never reached the caller as a
    # plan error. A node only means anything inside its own mission, which is
    # already how `depends_on` reads these ids.
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    mission_id: Mapped[str] = mapped_column(
        ForeignKey("sbot_missions.id"), primary_key=True, index=True
    )
    bot_id: Mapped[str | None] = mapped_column(ForeignKey("sbot_bots.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="task")  # task | map | reduce | review | gate | notify
    title: Mapped[str] = mapped_column(String(255))
    instruction: Mapped[str] = mapped_column(Text)
    depends_on: Mapped[list | None] = mapped_column(JSON, default=list)  # [node_id, ...]
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending | ready | running | done | error | skipped | awaiting_human
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts: Mapped[list | None] = mapped_column(JSON, default=list)
    budget: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost: Mapped[dict | None] = mapped_column(JSON, default=dict)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MissionBlackboard(Base):
    """MissionBlackboard: shared key-value context store for a mission."""

    __tablename__ = "sbot_mission_blackboard"
    __table_args__ = (
        Index("ix_sbot_mission_blackboard_key", "mission_id", "key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    mission_id: Mapped[str] = mapped_column(ForeignKey("sbot_missions.id"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[Any] = mapped_column(JSON)
    written_by_node: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


from claw.db.models import Skill, SkillSubscription


from claw.db.models import McpConnector


class Schedule(Base):
    """Recurring or one-shot prompt delivered to the agent on schedule."""

    __tablename__ = "sbot_schedules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # target chat
    name: Mapped[str] = mapped_column(String(128))
    cron: Mapped[str] = mapped_column(String(64), default="")  # cron expression, or
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)  # simple interval
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


from claw.db.models import UsageRecord


from claw.db.models import UsageDaily


class Feedback(Base):
    """User rating on an assistant reply — the raw signal for self-learning."""

    __tablename__ = "sbot_feedback"
    __table_args__ = (Index("ix_sbot_feedback_user_time", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal: Mapped[str] = mapped_column(String(8))  # up | down
    note: Mapped[str] = mapped_column(Text, default="")
    # Short preview of the rated reply, so later reflection has context without a join.
    message_preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


from claw.db.models import AuditEvent


from claw.db.models import LLMProvider


from claw.db.models import LLMModel


from claw.db.models import GuardrailRule


from claw.db.models import AppSetting


from claw.db.models import KnowledgeBase


from claw.db.models import KnowledgeBaseSharedGroup


from claw.db.models import KnowledgeDoc


from claw.db.models import KnowledgeChunk


class Blueprint(Base):
    """Reusable source document. Binary content lives below ``blueprints_root``;
    this row owns metadata and points at the active immutable version."""

    __tablename__ = "sbot_blueprints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private | group | public
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class BlueprintVersion(Base):
    """One immutable file revision of a Blueprint."""

    __tablename__ = "sbot_blueprint_versions"
    __table_args__ = (
        UniqueConstraint("blueprint_id", "version", name="uq_blueprint_version"),
        Index("ix_sbot_blueprint_versions_blueprint", "blueprint_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    blueprint_id: Mapped[str] = mapped_column(
        ForeignKey("sbot_blueprints.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(512))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Share(Base):
    """A public, read-only snapshot of one or more chat answers.

    Security model — capability URL: the link carries a high-entropy token;
    we store only its SHA-256 hash (like a password) so a DB leak can't
    reconstruct live links. The snapshot is an immutable copy taken at share
    time (never the live session) — later private messages in the same chat
    can never leak, and any referenced files are copied into a per-share
    directory served through a dedicated public route (never the owner-scoped
    workspace endpoint, which embeds the owner's token). Expires after a TTL
    and can be revoked instantly."""

    __tablename__ = "sbot_shares"
    __table_args__ = (Index("ix_sbot_shares_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Indexed via the explicit Index in __table_args__ above; no index=True here
    # or create_all would try to build the same-named index twice (SQLite errors).
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="Shared answer")
    # {"messages": [{"role", "content", "files": [{"name", "is_image"}]}]}
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
