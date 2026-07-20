"""SQLAlchemy models — one relational store for everything.

Messages are append-only; nothing ever rewrites a conversation file.
JSON columns use the portable JSON type (JSONB on Postgres via dialect).
"""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, func, text
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
    # How this account was created: password | google | microsoft | admin_created |
    # dev_token. Stamped once at creation time, purely informational (shown in
    # the Control Plane's Users list) — never a permission boundary. Existing
    # rows predating this column default to "password" (the oldest signup path),
    # which may not be accurate for accounts that actually first signed in via
    # OAuth before this was tracked.
    signup_method: Mapped[str] = mapped_column(String(16), default="password", server_default="password")
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
    # Usage-tier plan assigned directly to this user. Null = fall back to the
    # user's group plan, then the system default plan (see PolicyPlanStore.
    # resolve_for_user). Cleared to null when the plan is deleted.
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Last time an imported-pending-activation email was sent — used to rate
    # limit resends (see claw/api/auth.py's activation email helper). Null
    # until the first send.
    activation_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last time a "forgot password" reset email was sent — rate-limits resends
    # the same way activation_email_sent_at does.
    password_reset_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The nonce embedded in the currently-outstanding password-reset token (or
    # null if none is outstanding). Redeeming a reset token clears this via an
    # atomic compare-and-swap (UserStore.redeem_password_reset), so a token
    # can be redeemed at most once and a newly-requested reset invalidates
    # any prior unredeemed one.
    password_reset_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Personal appearance overrides (Settings > Profile > Preferences). Null =
    # no override, inherit the Control Plane's global branding default for
    # that field. Deliberately separate from `locale` above (which only feeds
    # the AI's reply language as a last-resort fallback) so changing the
    # global default never silently changes what "no override" means.
    ui_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    font_size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    chat_background: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        # Enforces (and indexes) case-insensitive email uniqueness — the
        # plain `unique=True` above is case-sensitive only, while lookups
        # throughout this codebase (UserStore.get_by_email()) are
        # case-insensitive, so this closes that gap at the DB layer too.
        Index("ix_users_email_lower", func.lower(email), unique=True),
    )


class UserGroup(Base):
    """A named group of users, for organization/filtering. A group may carry a
    default usage-tier plan (plan_id) that its members inherit when they have
    no plan of their own — otherwise it's purely organizational."""

    __tablename__ = "user_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # When true, self-registered users are placed in this group. At most one
    # group is the default at a time (enforced by the store).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Default usage-tier plan for members who have no per-user plan. Null =
    # fall through to the system default plan. Cleared when the plan is deleted.
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyPlan(Base):
    """A usage tier (Free/Plus/Pro/Max/Unlimited-style) governing which models a
    user may use (by cost ceiling) and their daily/per-minute quotas. Assigned
    per-user (User.plan_id) or per-group (UserGroup.plan_id); see
    PolicyPlanStore.resolve_for_user for the resolution order."""

    __tablename__ = "policy_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Display / privilege order (higher = more privileged); drives sort in the UI.
    rank: Mapped[int] = mapped_column(Integer, default=0)
    # Highest chat-model cost tier this plan may use (low|medium|high|very_high).
    # Global models above this are hidden from the picker and denied at resolve.
    max_chat_cost: Mapped[str] = mapped_column(String(16), default="very_high")
    # Whether the plan may generate images at all, and the image cost ceiling.
    allow_image: Mapped[bool] = mapped_column(Boolean, default=True)
    max_image_cost: Mapped[str] = mapped_column(String(16), default="very_high")
    # Daily quotas (0 = unlimited). messages = chat turns; images = generations.
    messages_per_day: Mapped[int] = mapped_column(Integer, default=0)
    images_per_day: Mapped[int] = mapped_column(Integer, default=0)
    # Per-minute chat-turn cap (0 = inherit global Settings.turns_per_minute).
    turns_per_minute: Mapped[int] = mapped_column(Integer, default=0)
    # Exactly one plan is the default (enforced by the store); it applies to
    # users/groups with no explicit plan.
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
    # Optional link to the MCP connector this skill's instructions rely on.
    # Lets the skill's content reference tools generically (e.g. "the
    # connected knowledge base") instead of hardcoding the connector's current
    # display name into `mcp_{name}_{tool}` strings — the runtime resolves the
    # connector's live, current tool names by this id every turn, so renaming
    # or recreating the connector never leaves the skill's text stale.
    # Cleared to null when the connector is deleted.
    connector_id: Mapped[str | None] = mapped_column(
        ForeignKey("mcp_connectors.id", ondelete="SET NULL"), nullable=True, index=True
    )
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


class UsageDaily(Base):
    """Per-day token rollup (day × user × model), maintained incrementally by
    UsageStore.record(). The Tokens Usage report queries THIS table, not the raw
    per-turn usage_records — so Daily/Weekly/Monthly/Yearly views stay cheap as
    turn volume grows (rows are bounded by days×users×models, not turns).
    Provider is derived from the live LLM config at query time, not stored here."""

    __tablename__ = "usage_daily"
    __table_args__ = (
        # The upsert key: one row per (day, user, model).
        Index("ix_usage_daily_key", "day", "user_id", "model", unique=True),
        # Range scans for a granularity window.
        Index("ix_usage_daily_day", "day"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    day: Mapped[date] = mapped_column(Date)
    # No FK (mirrors usage_records) so the rollup survives user deletion; the
    # UserStore.delete cascade prunes a user's rows explicitly.
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    # Text-to-image generations for this bucket (the /images path doesn't emit
    # tokens, so it's counted separately here for the images/day plan quota).
    images: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


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
    # "chat" = usable in the agent chat picker (sent tool definitions);
    # "image" = text-to-image only, used by the separate /images generation
    # path, never offered as a chat model (an image model can't do tool
    # calling, so picking it as a chat model would always fail).
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat|image
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
    bundles are visible only to their owner; `group` ones to the owner's
    current organizational group (User.group_id, resolved live) plus any
    groups listed in KnowledgeBaseSharedGroup; `public` ones to all users."""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private | group | public
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class KnowledgeBaseSharedGroup(Base):
    """Additional groups a `group`-visibility knowledge base is explicitly
    shared with, beyond the owner's own group (which is always included by
    default and is never a row here — see KnowledgeBase.visibility). Both FKs
    cascade so deleting either side cleans this up automatically."""

    __tablename__ = "knowledge_base_shared_groups"
    __table_args__ = (Index("ix_kb_shared_groups_group", "group_id"),)

    kb_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("user_groups.id", ondelete="CASCADE"), primary_key=True
    )


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
