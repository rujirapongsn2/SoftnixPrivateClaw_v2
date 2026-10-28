"""Pure half of OAuth for per-user remote MCP connectors: domain types, the
resource canonicalization every check shares, Bearer-challenge parsing, and the
grant codec. Nothing here touches the database or the network; SDK pydantic
models parse wire JSON at the boundary and never cross out of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp.client.auth.utils import (
    extract_field_from_www_auth,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
)

# A token that expires inside this window is refreshed ahead of time, so a
# request already in flight when it expires is not the one to find out.
EXPIRY_SKEW = timedelta(seconds=60)
TOKEN_TIMEOUT_S = 15.0
LEASE_TTL = timedelta(seconds=30)
assert TOKEN_TIMEOUT_S < LEASE_TTL.total_seconds(), "a refresh must finish inside its lease"
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024

_DEFAULT_PORTS = {"https": 443, "http": 80}


class AuthState(StrEnum):
    NEEDS_AUTHORIZATION = "needs_authorization"
    CONNECTED = "connected"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


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


def encode_grant(grant: Grant) -> str:
    kind = next(k for k, cls in _CLIENT_KINDS.items() if isinstance(grant.client, cls))
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
            "client": {"kind": kind, **grant.client.__dict__},
        }
    )


def decode_grant(raw: str) -> Grant:
    """Inverse of encode_grant. Raises ValueError on any malformed input, which
    the store maps to FailureReason.UNDECRYPTABLE."""
    try:
        data = json.loads(raw)
        client = dict(data["client"])
        client_cls = _CLIENT_KINDS[client.pop("kind")]
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
            client=client_cls(**client),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed grant") from exc
