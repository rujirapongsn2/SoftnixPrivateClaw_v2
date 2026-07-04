"""Application configuration — single source of truth, env-driven (CLAW_*)."""

from pathlib import Path

from pydantic import BaseModel
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


class SandboxSettings(BaseModel):
    """Tool-ephemeral sandbox: shell commands run in short-lived containers."""

    enabled: bool = True
    image: str = "python:3.12-slim"
    cpu_limit: float = 1.0
    memory_limit: str = "1g"
    pids_limit: int = 256
    network: str = "none"  # none | bridge
    timeout_seconds: int = 90


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

    # When false, the control policy runs in monitor-only mode (logs hits, no mask/block).
    policy_enforce: bool = True
    # Resource caps (bound in-memory growth at scale).
    max_resident_agents: int = 256
    max_session_locks: int = 2048
    # Per-user turn rate limit per minute (0 = unlimited).
    turns_per_minute: int = 60
    # Optional Telegram bot token; the channel starts only when set.
    telegram_bot_token: str = ""

    llm: LLMSettings = LLMSettings()
    sandbox: SandboxSettings = SandboxSettings()
    browser: BrowserSettings = BrowserSettings()


def load_settings() -> Settings:
    return Settings()
