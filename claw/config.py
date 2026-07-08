"""Application configuration — single source of truth, env-driven (CLAW_*)."""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseModel):
    model: str = "anthropic/claude-sonnet-4-5"
    api_key: str = ""
    api_base: str = ""
    max_tokens: int = 4096
    temperature: float = 0.1
    max_iterations: int = 30
    # Token budget for the assembled prompt (input side).
    max_context_tokens: int = 60_000


class BrowserSettings(BaseModel):
    """Server-side browser automation (Playwright). Off by default — requires the
    `browser` dependency group and `playwright install chromium`."""

    enabled: bool = False
    headless: bool = True
    timeout_seconds: int = 30
    max_chars: int = 30_000
    # Close a user's idle browser page after this many seconds (0 = keep until shutdown).
    idle_close_seconds: int = 600
    # Client-side browser: pair the user's own Chrome via the downloadable
    # extension. When a paired extension is online, the `browser` tool drives it
    # (real cookies/sessions); otherwise it falls back to the server-side one.
    client_extension_enabled: bool = False
    # How long the agent waits for the extension to run a queued task.
    poll_timeout_seconds: int = 60
    # Require admin approval before the extension runs a submit action.
    require_confirmation_for_submit: bool = False
    # Optional allow-list of domains the client browser may visit (empty = any).
    allowed_domains: list[str] = []


class SandboxSettings(BaseModel):
    """Tool-ephemeral sandbox: shell commands run in short-lived containers."""

    enabled: bool = True
    # Custom image pre-loaded with the document/archive stack (reportlab,
    # weasyprint, openpyxl, python-docx, python-pptx, pandas, zip/unzip).
    # Build with: docker build -f docker/sandbox.Dockerfile -t claw-sandbox:latest .
    image: str = "claw-sandbox:latest"
    cpu_limit: float = 1.0
    memory_limit: str = "1g"
    pids_limit: int = 256
    # bridge gives the sandbox internet (pip install, downloads); none isolates
    # it. bridge is more capable but riskier — exec runs are audit-logged.
    network: str = "bridge"  # none | bridge
    timeout_seconds: int = 90
    # Longer default: pip install / network fetches can be slow.
    # (kept modest to bound worst-case; raise if agents do heavy builds)


class MemorySettings(BaseModel):
    """Continuous-learning memory consolidation: the agent folds a session into
    durable per-user memory once enough new messages accumulate."""

    # New messages (user + assistant + tool) in a session before a consolidation
    # pass runs. Lower = the agent "learns" from shorter conversations. Tune via
    # CLAW_MEMORY__WINDOW.
    window: int = 30
    # Most-recent messages left raw (not yet folded into memory) each pass; must
    # be smaller than `window`. Tune via CLAW_MEMORY__KEEP.
    keep: int = 12


class KnowledgeSettings(BaseModel):
    """Knowledge-base ingestion (upload → parse → chunk → index)."""

    # Max size of a single uploaded document. Uploads stream to a staging file on
    # disk, so this is bounded by disk, not memory.
    max_doc_mb: int = 150
    # Files accepted per upload request (the web UI batches large selections to
    # stay under this). The real capacity comes from the background queue below.
    max_docs_per_upload: int = 10
    # How many documents the background ingest worker parses at once. Kept small
    # so ingestion never starves the event loop / chat responsiveness.
    ingest_concurrency: int = 2
    # OCR fallback for scanned/image-only PDFs. Off by default (needs the
    # `ocrmypdf` CLI + tesseract installed on the host). When on, a PDF that
    # yields almost no extractable text is run through OCR before chunking.
    ocr_enabled: bool = False
    # A PDF whose total extracted text is below this many characters is treated
    # as scanned (OCR candidate).
    ocr_min_chars: int = 20
    # Hard cap on how long a single OCR pass may run (seconds).
    ocr_timeout_seconds: int = 600


class SchedulerSettings(BaseModel):
    """Recurring/one-shot scheduled tasks."""

    # IANA timezone that cron expressions are interpreted in — e.g. cron
    # "0 7 * * *" means 07:00 in THIS zone, not UTC. Defaults to Asia/Bangkok so
    # local users get the wall-clock time they expect without configuring it.
    # Override with CLAW_SCHEDULER__TIMEZONE (e.g. UTC, America/New_York).
    timezone: str = "Asia/Bangkok"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAW_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://claw:claw@localhost:5432/claw"
    # Run Alembic migrations on startup (production). Tests/dev may create_all directly.
    auto_migrate: bool = True
    secret_key: str = "dev-secret-key-change-me"
    dev_token: str = "dev-token"
    # "dev" also accepts token+email (for scripts/tests); JWT bearer works in any mode.
    auth_mode: str = "dev"
    # Allow public self-registration. When false, only admins create users.
    open_registration: bool = True
    token_ttl_seconds: int = 7 * 24 * 3600

    # Public base URL of THIS API (for OIDC redirect_uri) and the web app to return to.
    public_base_url: str = "http://localhost:8700"
    web_base_url: str = "http://localhost:5173"
    # OIDC / social login — a provider is enabled when both client id and secret are set.
    oidc_google_client_id: str = ""
    oidc_google_client_secret: str = ""
    oidc_microsoft_client_id: str = ""
    oidc_microsoft_client_secret: str = ""
    oidc_microsoft_tenant: str = "common"
    host: str = "0.0.0.0"
    port: int = 8700
    # Root directory holding per-user agent workspaces.
    workspaces_root: Path = Path("workspaces")
    # Root directory holding knowledge-base OKF bundles (one subdir per base).
    knowledge_root: Path = Path("knowledge")

    # When false, the control policy runs in monitor-only mode (logs hits, no mask/block).
    policy_enforce: bool = True
    # Resource caps (bound in-memory growth at scale).
    max_resident_agents: int = 256
    max_session_locks: int = 2048
    # Per-user turn rate limit per minute (0 = unlimited).
    turns_per_minute: int = 60
    # Optional Telegram bot token; the channel starts only when set.
    telegram_bot_token: str = ""

    # Speech-to-text (Groq Whisper, OpenAI-compatible /audio/transcriptions).
    # Env names are un-prefixed (QROQ_*) by request, so read via explicit aliases
    # rather than the CLAW_ prefix. STT is enabled when the key is set.
    speech_api_key: str = Field(default="", validation_alias="QROQ_KEY")
    speech_api_base: str = Field(
        default="https://api.groq.com/openai/v1", validation_alias="QROQ_URL"
    )
    speech_model: str = Field(default="whisper-large-v3", validation_alias="QROQ_MODEL")

    llm: LLMSettings = LLMSettings()
    sandbox: SandboxSettings = SandboxSettings()
    browser: BrowserSettings = BrowserSettings()
    memory: MemorySettings = MemorySettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    knowledge: KnowledgeSettings = KnowledgeSettings()


def load_settings() -> Settings:
    return Settings()
