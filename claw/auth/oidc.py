"""OIDC / social login (Google, Microsoft) via the Authorization Code flow.

The network calls (token exchange, userinfo) take an injected httpx client so
the whole flow is unit-testable with a MockTransport — no live provider needed.
"""

import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from claw.auth.tokens import TokenError, decode_access_token, decode_unverified, encode
from claw.config import Settings

_STATE_TTL = 600  # seconds


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str = "openid email profile"


def _google(s: Settings) -> OIDCConfig:
    return OIDCConfig(
        name="google",
        client_id=s.oidc_google_client_id,
        client_secret=s.oidc_google_client_secret,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
    )


def _microsoft(s: Settings) -> OIDCConfig:
    tenant = s.oidc_microsoft_tenant or "common"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return OIDCConfig(
        name="microsoft",
        client_id=s.oidc_microsoft_client_id,
        client_secret=s.oidc_microsoft_client_secret,
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        userinfo_url="https://graph.microsoft.com/oidc/userinfo",
    )


def enabled_providers(settings: Settings) -> dict[str, OIDCConfig]:
    """Only providers with both a client id and secret configured are enabled."""
    out: dict[str, OIDCConfig] = {}
    for cfg in (_google(settings), _microsoft(settings)):
        if cfg.client_id and cfg.client_secret:
            out[cfg.name] = cfg
    return out


def provider_config(
    name: str, *, client_id: str, client_secret: str, tenant: str = "common"
) -> OIDCConfig | None:
    """Build a provider config from explicit credentials (e.g. an admin-registered
    OAuth app in the DB), independent of environment settings. Returns None for an
    unknown provider so the caller can fall through."""
    if name == "google":
        return OIDCConfig(
            name="google",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        )
    if name == "microsoft":
        base = f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0"
        return OIDCConfig(
            name="microsoft",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url=f"{base}/authorize",
            token_url=f"{base}/token",
            userinfo_url="https://graph.microsoft.com/oidc/userinfo",
        )
    return None


def redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/auth/oidc/{provider}/callback"


def make_state(provider: str, secret: str) -> str:
    return encode({"p": provider, "n": secrets.token_urlsafe(8)}, secret, _STATE_TTL)


def verify_state(state: str, provider: str, secret: str) -> bool:
    try:
        payload = decode_access_token(state, secret)
    except TokenError:
        return False
    return payload.get("p") == provider


def authorize_url(cfg: OIDCConfig, redirect: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": redirect,
        "scope": cfg.scope,
        "state": state,
        "response_mode": "query",
    }
    return f"{cfg.authorize_url}?{urlencode(params)}"


async def exchange_code(cfg: OIDCConfig, code: str, redirect: str, http: httpx.AsyncClient) -> dict:
    resp = await http.post(
        cfg.token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_identity(cfg: OIDCConfig, tokens: dict, http: httpx.AsyncClient) -> tuple[str, str]:
    """Return (email, display_name). Prefers the userinfo endpoint, falls back to
    id_token claims (the id_token came directly from the provider over TLS)."""
    email = ""
    name = ""
    access_token = tokens.get("access_token")
    if access_token:
        try:
            resp = await http.get(
                cfg.userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code == 200:
                info = resp.json()
                email = info.get("email") or info.get("preferred_username") or info.get("upn") or ""
                name = info.get("name") or ""
        except httpx.HTTPError:
            pass
    if not email and tokens.get("id_token"):
        claims = decode_unverified(tokens["id_token"])
        email = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
        name = name or claims.get("name") or ""
    return email.strip().lower(), name
