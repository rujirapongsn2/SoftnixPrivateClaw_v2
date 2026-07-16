"""Catalog of known MCP connectors so users can add popular integrations in one
click instead of hand-writing the command/URL. A preset is a template; the user
still supplies their own secrets (tokens/keys), which are encrypted at rest.

Connection method mirrors the reference project (softnix-agenticclaw):

* Local integrations run a **self-hosted MCP server** shipped in this repo
  (``claw/integrations/<name>_mcp_server.py``), launched over stdio with
  ``python -m claw.integrations.<name>_mcp_server``. The server reads its
  configuration (tokens, api base, …) from environment variables; non-secret
  settings default inside the server module.
* Remote integrations (Composio, Softnix ONE) connect to a hosted MCP endpoint
  over streamable HTTP. Their auth travels as an HTTP header, expressed as an
  env var prefixed ``HEADER_`` (e.g. ``HEADER_Authorization``); the connector
  manager turns those into request headers instead of process env.

Each preset carries a ``setup`` type and human-friendly ``fields`` so the web UI
can render a guided form (labels/help/secret) instead of exposing raw MCP config:

* ``api_key`` / ``token`` — the user pastes labeled secrets (``fields``).
* ``oauth`` — one-click "Connect with Google/Microsoft"; tokens are obtained via
  the OAuth flow (``oauth_provider`` + ``oauth_scopes``) and written under
  ``env_prefix`` (e.g. ``GMAIL_TOKEN``, ``GMAIL_REFRESH_TOKEN``, …). No fields.
"""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A single user-entered value in a guided connector setup form."""

    key: str  # env var name stored on the connector (HEADER_* → request header)
    label: str  # human-friendly field label
    help: str = ""  # short hint / "where to get this"
    secret: bool = True  # render as a password input
    optional: bool = False  # not required to save
    placeholder: str = ""
    # Stored value is prefixed with this (e.g. "Bearer ") if not already present,
    # so the user pastes only the token.
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class ConnectorPreset:
    key: str
    name: str  # default connector name to create
    label: str  # human title
    description: str
    transport: str  # stdio | http
    category: str = "Other"  # catalog group shown in the UI
    setup: str = "custom"  # api_key | token | oauth | custom
    command: str = ""
    url: str = ""
    # When True, `url` above is only a prefilled default — the guided setup
    # form shows an editable endpoint field so a self-hosted/per-tenant
    # deployment (e.g. a customer's own Softnix ONE instance) can point
    # elsewhere. False (default) hides the field, for presets with one true
    # fixed endpoint shared by every user (e.g. Composio's public gateway).
    url_configurable: bool = False
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    docs: str = ""
    # OAuth presets only:
    oauth_provider: str = ""  # google | microsoft
    oauth_scopes: str = ""
    env_prefix: str = ""  # e.g. GMAIL — tokens stored as GMAIL_TOKEN, GMAIL_REFRESH_TOKEN, …

    def to_dict(self) -> dict:
        return asdict(self)


def _server(module: str) -> str:
    """Command that launches a built-in stdio MCP server module."""
    return f"python -m claw.integrations.{module}"


# Built-in self-hosted MCP servers (stdio) + hosted remote endpoints (http).
_PRESETS: tuple[ConnectorPreset, ...] = (
    ConnectorPreset(
        key="github",
        name="github",
        label="GitHub",
        description="Repositories, issues, pull requests, and code search.",
        transport="stdio",
        category="Productivity",
        setup="api_key",
        command=_server("github_mcp_server"),
        fields=(
            FieldSpec(
                key="GITHUB_TOKEN",
                label="Personal access token",
                help="Create one at github.com/settings/tokens (needs repo access).",
                placeholder="ghp_…",
            ),
            FieldSpec(
                key="GITHUB_DEFAULT_REPO",
                label="Default repository",
                help="Optional. owner/name to use when you don't specify one.",
                secret=False,
                optional=True,
                placeholder="octocat/hello-world",
            ),
        ),
        docs="https://github.com/settings/tokens",
    ),
    ConnectorPreset(
        key="notion",
        name="notion",
        label="Notion",
        description="Search, read, and update Notion pages and databases.",
        transport="stdio",
        category="Productivity",
        setup="api_key",
        command=_server("notion_mcp_server"),
        fields=(
            FieldSpec(
                key="NOTION_TOKEN",
                label="Internal integration secret",
                help="Create an integration at notion.so/my-integrations, then share your pages with it.",
                placeholder="secret_…",
            ),
            FieldSpec(
                key="NOTION_DEFAULT_PAGE_ID",
                label="Default page ID",
                help="Optional. A page to use by default.",
                secret=False,
                optional=True,
            ),
        ),
        docs="https://www.notion.so/my-integrations",
    ),
    ConnectorPreset(
        key="onedrive",
        name="onedrive",
        label="OneDrive",
        description="Browse, search, read, and share files in OneDrive.",
        transport="stdio",
        category="Productivity",
        setup="oauth",
        command=_server("onedrive_mcp_server"),
        oauth_provider="microsoft",
        oauth_scopes="offline_access openid email https://graph.microsoft.com/Files.ReadWrite.All",
        env_prefix="ONEDRIVE",
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="gmail",
        name="gmail",
        label="Gmail",
        description="Search and read Gmail, manage labels, draft and send mail.",
        transport="stdio",
        category="Communication",
        setup="oauth",
        command=_server("gmail_mcp_server"),
        oauth_provider="google",
        oauth_scopes="openid email https://www.googleapis.com/auth/gmail.modify",
        env_prefix="GMAIL",
        docs="https://developers.google.com/gmail/api",
    ),
    ConnectorPreset(
        key="outlook",
        name="outlook",
        label="Outlook / Microsoft 365 Mail",
        description="Microsoft 365 mail, folders, drafts, and send via Graph.",
        transport="stdio",
        category="Communication",
        setup="oauth",
        command=_server("outlook_mcp_server"),
        oauth_provider="microsoft",
        oauth_scopes="offline_access openid email https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/User.Read",
        env_prefix="OUTLOOK",
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="outlook-calendar",
        name="outlook-calendar",
        label="Outlook Calendar",
        description="Microsoft 365 calendar events, search, and scheduling.",
        transport="stdio",
        category="Communication",
        setup="oauth",
        command=_server("outlook_calendar_mcp_server"),
        oauth_provider="microsoft",
        oauth_scopes="offline_access openid https://graph.microsoft.com/Calendars.ReadWrite",
        env_prefix="OUTLOOK_CALENDAR",
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="tavily",
        name="tavily",
        label="Tavily Search",
        description="Web search and page extraction for current information.",
        transport="stdio",
        category="Search",
        setup="api_key",
        command=_server("tavily_mcp_server"),
        fields=(
            FieldSpec(
                key="TAVILY_API_KEY",
                label="API key",
                help="Get a free key at app.tavily.com.",
                placeholder="tvly-…",
            ),
        ),
        docs="https://app.tavily.com/",
    ),
    ConnectorPreset(
        key="composio",
        name="composio",
        label="Composio",
        description="Third-party app actions via Composio's hosted MCP endpoint.",
        transport="http",
        category="Automation",
        setup="token",
        url="https://connect.composio.dev/mcp",
        fields=(
            FieldSpec(
                key="HEADER_x-consumer-api-key",
                label="Composio API key",
                help="Find it in your Composio dashboard.",
            ),
        ),
        docs="https://composio.dev/",
    ),
    ConnectorPreset(
        key="softnix-one",
        name="softnix-one",
        label="Softnix ONE",
        description="Softnix ONE tasks, leads, notes, meetings, and AI knowledge.",
        transport="http",
        category="Softnix",
        setup="token",
        url="https://mcp-softnix-one.softnix.ai/mcp",
        # Self-hosted/on-prem Softnix ONE deployments live at a customer-specific
        # URL, unlike Composio's single shared public gateway — let the form
        # override this default instead of hardcoding one endpoint for everyone.
        url_configurable=True,
        fields=(
            FieldSpec(
                key="HEADER_Authorization",
                label="Softnix ONE API token",
                help="Paste your token — the 'Bearer' prefix is added automatically.",
                prefix="Bearer ",
            ),
        ),
        docs="https://softnix.ai/",
    ),
)

_BY_KEY = {p.key: p for p in _PRESETS}


def list_presets() -> list[dict]:
    return [p.to_dict() for p in _PRESETS]


def get_preset(key: str) -> ConnectorPreset | None:
    return _BY_KEY.get(key)


# The exact `command` string of every built-in stdio preset (always
# `python -m claw.integrations.<module>` — developer-authored code, never
# user input). A non-admin's stdio connector must match one of these exactly;
# see claw/api/manage.py::upsert_connector. Anything else is an ordinary
# user's own command string, which would let it run arbitrary unsandboxed
# subprocesses on the host — that stays admin-only.
_ALLOWED_STDIO_COMMANDS: frozenset[str] = frozenset(
    p.command for p in _PRESETS if p.transport == "stdio"
)


def is_allowed_stdio_command(command: str) -> bool:
    return command.strip() in _ALLOWED_STDIO_COMMANDS
