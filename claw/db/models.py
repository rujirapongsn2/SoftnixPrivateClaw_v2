"""SQLAlchemy models — one relational store for everything.

Messages are append-only; nothing ever rewrites a conversation file.
JSON columns use the portable JSON type (JSONB on Postgres via dialect).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    # Display label ("admin"/"user"). System-admin capability is is_admin, not role.
    role: Mapped[str] = mapped_column(String(16), default="user")
    # Cross-user/system administration (manage other users, global policy).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Suspended users cannot authenticate.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str] = mapped_column(String(8), default="en")
    # Proactive check-in cadence; 0 disables the heartbeat for this user.
    heartbeat_interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Linked Telegram account id (null = not linked). Unique so one Telegram maps to one user.
    telegram_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    # Organizational group (purely for management/filtering — NOT a policy or
    # permission boundary). Null = ungrouped; cleared to null when the group is
    # deleted (the user is kept).
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserGroup(Base):
    """A named group of users, for organization/filtering only. Carries no
    policy or permission meaning."""

    __tablename__ = "user_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # When true, self-registered users are placed in this group. At most one
    # group is the default at a time (enforced by the store).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="New chat")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    # Sticky per-chat model choice (litellm id). Null = use the configured default.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    last_consolidated_seq: Mapped[int] = mapped_column(Integer, default=0)
    # Working plan for the current task: {"goal": str, "steps": [{"step", "status"}]}.
    # Pinned into the system prompt every turn (never trimmed) so the agent keeps
    # the thread on long/autonomous runs even after early messages scroll out of
    # context. Maintained by the agent via the `update_plan` tool. Null = no plan.
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_seq", "session_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per session
    role: Mapped[str] = mapped_column(String(16))  # user|assistant|tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Memory(Base):
    """Two-layer memory: kind='core' (one living doc per user) and kind='history' entries."""

    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_user_kind", "user_id", "kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # core|history
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Skill(Base):
    """User-authored capability: description goes in the system prompt, content on demand."""

    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_user_name", "user_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class McpConnector(Base):
    """MCP server config per user; tools register as mcp_{name}_{tool}."""

    __tablename__ = "mcp_connectors"
    __table_args__ = (Index("ix_connectors_user_name", "user_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    transport: Mapped[str] = mapped_column(String(16), default="stdio")  # stdio|http
    command: Mapped[str] = mapped_column(Text, default="")  # stdio: command line
    url: Mapped[str] = mapped_column(String(500), default="")  # http: endpoint
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Schedule(Base):
    """Recurring or one-shot prompt delivered to the agent on schedule."""

    __tablename__ = "schedules"

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


class UsageRecord(Base):
    """Per-turn LLM token usage, for cost tracking and quotas."""

    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_user_time", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Feedback(Base):
    """User rating on an assistant reply — the raw signal for self-learning."""

    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_user_time", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal: Mapped[str] = mapped_column(String(8))  # up | down
    note: Mapped[str] = mapped_column(Text, default="")
    # Short preview of the rated reply, so later reflection has context without a join.
    message_preview: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_kind_time", "kind", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))  # tool_call|message|auth|policy
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMProvider(Base):
    """An upstream LLM provider (OpenRouter, Anthropic, …).

    The API key is stored encrypted at rest (SecretBox), like connector secrets.

    Ownership scope: ``owner_id`` NULL means an admin-global provider (configured
    in the Control Plane, offered to everyone). A non-null ``owner_id`` is a
    private "bring your own key" provider owned by that user — visible and usable
    only by them. Names are unique per owner (a partial unique index enforces
    global-name uniqueness among the NULL-owner rows, since Postgres treats NULLs
    as distinct), mirroring the (user_id, name) pattern on Skill/McpConnector.
    """

    __tablename__ = "llm_providers"
    __table_args__ = (
        Index("ix_llm_providers_owner_name", "owner_id", "name", unique=True),
        Index(
            "ix_llm_providers_global_name",
            "name",
            unique=True,
            postgresql_where=text("owner_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # NULL = admin-global; set = private to this user (BYOK).
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    api_key: Mapped[str] = mapped_column(Text, default="")  # encrypted at rest
    api_base: Mapped[str] = mapped_column(String(500), default="")
    # LiteLLM routing prefix (e.g. "openai", "anthropic", "openrouter") applied
    # automatically ahead of every model id added under this provider, so admins
    # type the model's real name (whatever the vendor/gateway documents) instead
    # of a LiteLLM implementation detail. Blank on providers created before this
    # existed — those fall back to typing the full id manually.
    model_prefix: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMModel(Base):
    """A model a provider exposes; enabled ones appear in the chat model picker."""

    __tablename__ = "llm_models"
    __table_args__ = (Index("ix_llm_models_provider", "provider_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    provider_id: Mapped[str] = mapped_column(ForeignKey("llm_providers.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(128))  # litellm id, e.g. anthropic/claude-sonnet-5
    label: Mapped[str] = mapped_column(String(128), default="")
    # Shown in the chat model picker: cost tier + a one-line description.
    cost: Mapped[str] = mapped_column(String(16), default="medium")  # low|medium|high|very_high
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class GuardrailRule(Base):
    """A control-policy rule (mask/block/monitor). Built-ins are seeded on first run;
    admins can toggle them and add custom keyword/regex rules."""

    __tablename__ = "guardrail_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    pattern: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16), default="mask")  # mask|block|monitor
    scopes: Mapped[list] = mapped_column(JSON, default=lambda: ["input", "output", "tool_args"])
    placeholder: Mapped[str] = mapped_column(String(64), default="[REDACTED]")
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    block_message: Mapped[str] = mapped_column(
        Text, default="This request was blocked by the control policy."
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AppSetting(Base):
    """Tiny key/value store for admin-tunable settings persisted across restarts."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class KnowledgeBase(Base):
    """A user-created knowledge collection (an OKF "bundle"). Documents uploaded
    into it are parsed, chunked, and made searchable by the agent. `private`
    bundles are visible only to their owner; `public` ones to all users."""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private | public
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class KnowledgeDoc(Base):
    """One uploaded source document within a knowledge base — an OKF "concept"
    markdown file. `concept_id` is its path within the bundle (minus `.md`)."""

    __tablename__ = "knowledge_docs"
    __table_args__ = (Index("ix_kdocs_kb", "kb_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    concept_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    mime: Mapped[str] = mapped_column(String(120), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    # Ingestion lifecycle: pending → processing → ready | failed. Documents are
    # parsed by a background worker, so a freshly uploaded doc is "pending" until
    # the worker finishes. server_default 'ready' keeps every pre-existing row
    # (already fully ingested) valid after the migration.
    status: Mapped[str] = mapped_column(String(16), default="ready", server_default="ready")
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KnowledgeChunk(Base):
    """A retrievable slice of a document's text. Searched via pg_trgm word
    similarity (language-agnostic — works for Thai and English without an
    embedding model). A GIN trigram index on `text` is added in the migration."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_kchunks_kb", "kb_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("knowledge_docs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    # 1-based source page this chunk came from (PDF); null for formats without
    # pages (docx/html/txt) or chunks ingested before page tracking existed.
    # Used only to enrich citations — never affects retrieval.
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
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

    __tablename__ = "shares"
    __table_args__ = (Index("ix_shares_user_id", "user_id"),)

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
