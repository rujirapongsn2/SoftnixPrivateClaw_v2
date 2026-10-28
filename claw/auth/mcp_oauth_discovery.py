"""Domain half of OAuth for per-user remote MCP connectors: domain types, the
resource canonicalization every check shares, Bearer-challenge parsing, the
grant and pending-flow codecs, and discovery. Nothing here touches the
database. `discover` is the only network code, and it dials only through the
client it is handed; SDK pydantic models parse wire JSON at the boundary and
never cross out of it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_field_from_www_auth,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError

from claw.security.ssrf import ResponseTooLarge, UnsafeUrlError, read_bounded

# A token that expires inside this window is refreshed ahead of time, so a
# request already in flight when it expires is not the one to find out.
EXPIRY_SKEW = timedelta(seconds=60)
TOKEN_TIMEOUT_S = 15.0
LEASE_TTL = timedelta(seconds=30)
assert TOKEN_TIMEOUT_S < LEASE_TTL.total_seconds(), "a refresh must finish inside its lease"
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
MAX_METADATA_BYTES = 64 * 1024
# How long a consent flow may sit at the authorization server before its
# state stops being accepted.
PENDING_TTL = timedelta(minutes=10)
CALLBACK_PATH = "/api/mcp-oauth/callback"
# Matched by claw/core/connectors.py's _OAUTH_REAUTH_MARKERS, so a failure
# raised through a live tool call flips the connector's status the same way a
# preset connector's "invalid_grant" does, with no agent-loop change.
REAUTH_MARKER = "mcp_oauth_reauthorization_required"

_DEFAULT_PORTS = {"https": 443, "http": 80}


class AuthState(StrEnum):
    NEEDS_AUTHORIZATION = "needs_authorization"
    CONNECTED = "connected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class CallbackOutcome(StrEnum):
    """What the browser is told after the authorization server sends it back;
    the frontend maps each value to its own copy."""

    CONNECTED = "connected"
    DENIED = "denied"
    EXPIRED = "expired"
    BROWSER_MISMATCH = "browser_mismatch"
    TOKEN_FAILED = "token_failed"
    ERROR = "error"


class ReturnTo(StrEnum):
    """Which web app started the flow, so the callback lands back in it."""

    CLAW = "claw"
    SBOT = "sbot"


class ClientStrategy(StrEnum):
    ADMIN = "admin"


def choose_client_strategy(*, has_admin_client: bool) -> ClientStrategy | None:
    """How to identify ourselves to an authorization server. Only an
    admin-registered client exists today; Client ID Metadata Documents and
    Dynamic Client Registration slot in here, after ADMIN, as fallbacks."""
    return ClientStrategy.ADMIN if has_admin_client else None


class FailureReason(StrEnum):
    """Persisted in mcp_connector_auth.failure; its presence is what makes the
    state reauthorization_required."""

    INVALID_GRANT = "invalid_grant"
    INVALID_CLIENT = "invalid_client"
    CLIENT_GONE = "client_gone"
    REJECTED_TOKEN = "rejected_token"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    UNDECRYPTABLE = "undecryptable"


@dataclass(frozen=True)
class AdminClientRef:
    row_id: str


@dataclass(frozen=True)
class RegisteredClientRef:
    row_id: str


@dataclass(frozen=True)
class MetadataDocClientRef:
    client_id: str


ClientRef = AdminClientRef | RegisteredClientRef | MetadataDocClientRef


@dataclass(frozen=True)
class ClientCredentials:
    """Resolved from a ClientRef at use time; never persisted in this shape."""

    client_id: str
    client_secret: str | None
    auth_method: str  # client_secret_basic | client_secret_post | none


@dataclass(frozen=True)
class BearerChallenge:
    resource_metadata: str | None
    scope: str | None
    error: str | None


def parse_bearer_challenge(response: httpx.Response) -> BearerChallenge | None:
    """The Bearer challenge on a 401/403, or None when the response is not one."""
    if response.status_code not in (401, 403):
        return None
    header = response.headers.get("WWW-Authenticate", "")
    if not header.lower().startswith("bearer"):
        return None
    return BearerChallenge(
        resource_metadata=extract_resource_metadata_from_www_auth(response),
        scope=extract_scope_from_www_auth(response),
        error=extract_field_from_www_auth(response, "error"),
    )


def canonical_resource(server_url: str) -> str:
    """The RFC 8707 resource identifier for an MCP server URL.

    Lowercase scheme and host, default port dropped, query and fragment
    dropped, one trailing slash stripped. The same function serves the
    `resource` request parameter and the grant-to-connector binding check, so
    the two cannot disagree."""
    parts = urlsplit(server_url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port is None or _DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return urlunsplit((scheme, netloc, path, "", ""))


@dataclass(frozen=True)
class Grant:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None
    issuer: str
    resource: str
    token_endpoint: str
    revocation_endpoint: str | None
    client: ClientRef

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at - EXPIRY_SKEW > now


def auth_state(grant: Grant | None, failure: FailureReason | None) -> AuthState:
    """The one place a connector's OAuth state is derived; nothing stores it."""
    if failure is not None:
        return AuthState.REAUTHORIZATION_REQUIRED
    if grant is None:
        return AuthState.NEEDS_AUTHORIZATION
    return AuthState.CONNECTED


_CLIENT_KINDS: dict[str, type] = {
    "admin": AdminClientRef,
    "dcr": RegisteredClientRef,
    "cimd": MetadataDocClientRef,
}


def _encode_client(client: ClientRef) -> dict:
    kind = next(k for k, cls in _CLIENT_KINDS.items() if isinstance(client, cls))
    return {"kind": kind, **client.__dict__}


def _decode_client(data: dict) -> ClientRef:
    data = dict(data)
    return _CLIENT_KINDS[data.pop("kind")](**data)


def encode_grant(grant: Grant) -> str:
    return json.dumps(
        {
            "access_token": grant.access_token,
            "refresh_token": grant.refresh_token,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            "scope": grant.scope,
            "issuer": grant.issuer,
            "resource": grant.resource,
            "token_endpoint": grant.token_endpoint,
            "revocation_endpoint": grant.revocation_endpoint,
            "client": _encode_client(grant.client),
        }
    )


def decode_grant(raw: str) -> Grant:
    """Inverse of encode_grant. Raises ValueError on any malformed input, which
    the store maps to FailureReason.UNDECRYPTABLE."""
    try:
        data = json.loads(raw)
        expires_at = data["expires_at"]
        return Grant(
            access_token=str(data["access_token"]),
            refresh_token=data["refresh_token"],
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            scope=data["scope"],
            issuer=str(data["issuer"]),
            resource=str(data["resource"]),
            token_endpoint=str(data["token_endpoint"]),
            revocation_endpoint=data["revocation_endpoint"],
            client=_decode_client(data["client"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed grant") from exc


@dataclass(frozen=True)
class AuthView:
    """What the UI may know about a connector's authorization. Deliberately
    carries no token, endpoint, or client material."""

    state: AuthState
    issuer: str | None
    scope: str | None
    can_revoke: bool
    failure: FailureReason | None


def auth_view(grant: Grant | None, failure: FailureReason | None) -> AuthView:
    return AuthView(
        state=auth_state(grant, failure),
        issuer=grant.issuer if grant else None,
        scope=grant.scope if grant else None,
        can_revoke=bool(grant and grant.revocation_endpoint),
        failure=failure,
    )


@dataclass(frozen=True)
class PendingFlow:
    """One in-flight consent flow, stored encrypted until its callback. The
    PKCE verifier never leaves the server; the browser holds only `state` and
    the nonce cookie, and only their hashes are kept."""

    code_verifier: str
    browser_nonce_hash: str
    issuer: str
    resource: str
    token_endpoint: str
    revocation_endpoint: str | None
    client: ClientRef
    redirect_uri: str
    scope: str | None
    return_to: ReturnTo


def encode_pending(pending: PendingFlow) -> str:
    data = asdict(pending)
    data["client"] = _encode_client(pending.client)
    return json.dumps(data)


def decode_pending(raw: str) -> PendingFlow:
    try:
        data = json.loads(raw)
        data["client"] = _decode_client(data["client"])
        data["return_to"] = ReturnTo(data["return_to"])
        return PendingFlow(**data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed pending flow") from exc


@dataclass(frozen=True)
class Discovered:
    """Validated authorization facts for one MCP server: every endpoint is
    https, the issuer matched its metadata, and S256 is supported."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    scope: str | None


class DiscoveryError(Exception):
    """Discovery failed. The message is safe to show the user: it names hosts
    the user configured or the server advertised, never response bodies."""


async def discover(http: httpx.AsyncClient, server_url: str) -> Discovered:
    """Run MCP authorization discovery for `server_url` through `http`.

    The caller supplies a client that pins every dial to a validated public
    https address and never follows redirects; a 3xx anywhere is refused here
    rather than chased, since its target is a URL the peer chose."""
    resource = canonical_resource(server_url)
    challenge = await _probe(http, server_url)

    prm: ProtectedResourceMetadata | None = None
    for url in build_protected_resource_metadata_discovery_urls(challenge.resource_metadata, server_url):
        body = await _fetch_metadata(http, url)
        if body is None:
            continue
        try:
            prm = ProtectedResourceMetadata.model_validate_json(body)
        except ValidationError as exc:
            raise DiscoveryError(f"{_host(url)} returned invalid protected resource metadata.") from exc
        break
    if prm is None:
        raise DiscoveryError(f"{_host(server_url)} does not publish OAuth protected resource metadata.")
    if not check_resource_allowed(resource, canonical_resource(str(prm.resource))):
        raise DiscoveryError(f"{_host(server_url)} advertised metadata for a different resource.")

    as_url = str(prm.authorization_servers[0])
    _require_https("authorization server", as_url)
    metadata: OAuthMetadata | None = None
    for url in build_oauth_authorization_server_metadata_discovery_urls(as_url, server_url):
        body = await _fetch_metadata(http, url)
        if body is None:
            continue
        try:
            metadata = OAuthMetadata.model_validate_json(body)
        except ValidationError as exc:
            raise DiscoveryError(f"{_host(url)} returned invalid authorization server metadata.") from exc
        break
    if metadata is None:
        raise DiscoveryError(f"{_host(as_url)} does not publish authorization server metadata.")

    issuer = str(metadata.issuer).rstrip("/")
    # RFC 8414 §3.3: metadata whose issuer differs from the URL it was
    # discovered under may be another server's, replayed.
    if issuer != as_url.rstrip("/"):
        raise DiscoveryError(f"{_host(as_url)} published metadata for a different issuer.")
    # The MCP spec requires refusing to proceed without S256: an absent list
    # means PKCE support cannot be confirmed.
    if "S256" not in (metadata.code_challenge_methods_supported or []):
        raise DiscoveryError(f"{_host(as_url)} does not support PKCE with S256.")
    endpoints = {
        "authorization endpoint": str(metadata.authorization_endpoint),
        "token endpoint": str(metadata.token_endpoint),
    }
    if metadata.revocation_endpoint is not None:
        endpoints["revocation endpoint"] = str(metadata.revocation_endpoint)
    for label, url in endpoints.items():
        _require_https(label, url)

    return Discovered(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=endpoints["authorization endpoint"],
        token_endpoint=endpoints["token endpoint"],
        revocation_endpoint=endpoints.get("revocation endpoint"),
        scope=get_client_metadata_scopes(challenge.scope, prm),
    )


async def _probe(http: httpx.AsyncClient, server_url: str) -> BearerChallenge:
    """An unauthenticated MCP initialize, which an OAuth-protected server
    answers with 401 and a Bearer challenge."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "claw", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    try:
        async with http.stream("POST", server_url, json=initialize, headers=headers) as response:
            _refuse_redirect(response, server_url)
            challenge = parse_bearer_challenge(response)
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        raise DiscoveryError(_unreachable(server_url, exc)) from exc
    if response.status_code != 401 or challenge is None:
        raise DiscoveryError(f"{_host(server_url)} did not ask for OAuth authorization.")
    return challenge


async def _fetch_metadata(http: httpx.AsyncClient, url: str) -> bytes | None:
    """The body of a 200 metadata document, or None when this candidate URL
    has none (the next well-known URL is tried)."""
    try:
        async with http.stream("GET", url, headers={"Accept": "application/json"}) as response:
            _refuse_redirect(response, url)
            if response.status_code != 200:
                return None
            return await read_bounded(response, MAX_METADATA_BYTES)
    except ResponseTooLarge as exc:
        raise DiscoveryError(f"{_host(url)} returned an oversized metadata document.") from exc
    except (httpx.HTTPError, UnsafeUrlError) as exc:
        raise DiscoveryError(_unreachable(url, exc)) from exc


def _refuse_redirect(response: httpx.Response, url: str) -> None:
    if response.is_redirect or 300 <= response.status_code < 400:
        raise DiscoveryError(f"{_host(url)} redirected during authorization discovery, which is not allowed.")


def _require_https(label: str, url: str) -> None:
    if urlsplit(url).scheme.lower() != "https":
        raise DiscoveryError(f"The {label} {_host(url)} is not served over HTTPS.")


def _unreachable(url: str, exc: Exception) -> str:
    if isinstance(exc, UnsafeUrlError):
        return f"{_host(url)} is not a public HTTPS address."
    return f"Could not reach {_host(url)}."


def _host(url: str) -> str:
    return urlsplit(url).hostname or url
