"""One-click connector OAuth (Google / Microsoft).

Mirrors the login flow in ``claw.auth.oidc`` but for connectors:
- uses the OAuth app credentials an admin registered (``OAuthAppStore``),
- requests **offline access** so we get a refresh token (the MCP servers refresh
  on their own using it),
- requests the connector-specific scopes from its preset,
- the signed ``state`` carries the user id + preset key, and the callback creates
  the connector instead of issuing a login JWT.

Network calls take an injected httpx client so the flow is unit-testable with a
MockTransport (no live provider needed).
"""

from urllib.parse import urlencode

import httpx

from claw.auth.tokens import TokenError, decode_access_token, encode
from claw.config import Settings
from claw.core.connector_presets import ConnectorPreset

_STATE_TTL = 600  # seconds

_PROVIDER = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    },
    # Microsoft's URLs are tenant-specific; resolved in _endpoints().
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    },
}


def _endpoints(provider: str, tenant: str) -> tuple[str, str]:
    p = _PROVIDER[provider]
    t = (tenant or "common") if provider == "microsoft" else ""
    return p["authorize_url"].format(tenant=t), p["token_url"].format(tenant=t)


def redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/connectors/oauth/{provider}/callback"


def make_state(user_id: str, preset_key: str, provider: str, secret: str) -> str:
    return encode({"u": user_id, "k": preset_key, "p": provider}, secret, _STATE_TTL)


def read_state(state: str, secret: str) -> dict | None:
    try:
        payload = decode_access_token(state, secret)
    except TokenError:
        return None
    if not payload.get("u") or not payload.get("k") or not payload.get("p"):
        return None
    return payload


def authorize_url(preset: ConnectorPreset, app: dict, settings: Settings, state: str) -> str:
    """Build the provider authorize URL for this connector's scopes + offline access."""
    provider = preset.oauth_provider
    auth_url, _ = _endpoints(provider, app.get("tenant", ""))
    params = {
        "response_type": "code",
        "client_id": app["client_id"],
        "redirect_uri": redirect_uri(settings, provider),
        "scope": preset.oauth_scopes,
        "state": state,
        "response_mode": "query",
    }
    if provider == "google":
        # Required for Google to return a refresh_token.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    return f"{auth_url}?{urlencode(params)}"


async def exchange_code(
    preset: ConnectorPreset, app: dict, code: str, redirect: str, http: httpx.AsyncClient
) -> dict:
    _, token_url = _endpoints(preset.oauth_provider, app.get("tenant", ""))
    resp = await http.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
        },
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def tokens_to_env(preset: ConnectorPreset, app: dict, tokens: dict) -> dict[str, str]:
    """Map an OAuth token response onto the connector's env vars so its MCP server
    can authenticate and self-refresh."""
    _, token_url = _endpoints(preset.oauth_provider, app.get("tenant", ""))
    prefix = preset.env_prefix
    env = {
        f"{prefix}_TOKEN": tokens.get("access_token", ""),
        f"{prefix}_REFRESH_TOKEN": tokens.get("refresh_token", ""),
        f"{prefix}_CLIENT_ID": app["client_id"],
        f"{prefix}_CLIENT_SECRET": app["client_secret"],
        f"{prefix}_TOKEN_URI": token_url,
    }
    if preset.oauth_provider == "microsoft":
        env[f"{prefix}_TENANT_ID"] = app.get("tenant", "") or "common"
    return {k: v for k, v in env.items() if v}
