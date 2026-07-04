"""Catalog of known MCP connectors so users can add popular integrations in one
click instead of hand-writing the command/URL. A preset is a template; the user
still supplies their own secrets (tokens/keys), which are encrypted at rest."""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
class ConnectorPreset:
    key: str
    name: str  # default connector name to create
    label: str  # human title
    description: str
    transport: str  # stdio | http
    command: str = ""
    url: str = ""
    # Env var names the user must fill in (values are secret, encrypted at rest).
    env_fields: tuple[str, ...] = field(default_factory=tuple)
    docs: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# npx-based reference servers + hosted HTTP endpoints. Commands assume Node/npx
# is available in the agent host; users can edit after adding.
_PRESETS: tuple[ConnectorPreset, ...] = (
    ConnectorPreset(
        key="github",
        name="github",
        label="GitHub",
        description="Repositories, issues, pull requests, and code search.",
        transport="stdio",
        command="npx -y @modelcontextprotocol/server-github",
        env_fields=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    ),
    ConnectorPreset(
        key="gmail",
        name="gmail",
        label="Gmail",
        description="Read and search Gmail, send mail, manage labels.",
        transport="stdio",
        command="npx -y @gongrzhe/server-gmail-autoauth-mcp",
        env_fields=("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN"),
        docs="https://github.com/gongrzhe/server-gmail-autoauth-mcp",
    ),
    ConnectorPreset(
        key="outlook",
        name="outlook",
        label="Outlook / Microsoft 365 Mail",
        description="Microsoft 365 mail, folders, and messages via Graph.",
        transport="stdio",
        command="npx -y @modelcontextprotocol/server-outlook",
        env_fields=("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"),
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="outlook-calendar",
        name="outlook-calendar",
        label="Outlook Calendar",
        description="Microsoft 365 calendar events and scheduling.",
        transport="stdio",
        command="npx -y @modelcontextprotocol/server-outlook-calendar",
        env_fields=("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"),
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="onedrive",
        name="onedrive",
        label="OneDrive",
        description="Browse and read files in OneDrive.",
        transport="stdio",
        command="npx -y @modelcontextprotocol/server-onedrive",
        env_fields=("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"),
        docs="https://learn.microsoft.com/graph/",
    ),
    ConnectorPreset(
        key="notion",
        name="notion",
        label="Notion",
        description="Read and update Notion pages and databases.",
        transport="stdio",
        command="npx -y @notionhq/notion-mcp-server",
        env_fields=("NOTION_API_KEY",),
        docs="https://github.com/makenotion/notion-mcp-server",
    ),
    ConnectorPreset(
        key="tavily",
        name="tavily",
        label="Tavily Search",
        description="Web search and page extraction for current information.",
        transport="stdio",
        command="npx -y tavily-mcp",
        env_fields=("TAVILY_API_KEY",),
        docs="https://github.com/tavily-ai/tavily-mcp",
    ),
)

_BY_KEY = {p.key: p for p in _PRESETS}


def list_presets() -> list[dict]:
    return [p.to_dict() for p in _PRESETS]


def get_preset(key: str) -> ConnectorPreset | None:
    return _BY_KEY.get(key)
