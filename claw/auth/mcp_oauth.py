"""OAuth for per-user remote MCP connectors: the service that owns grants and
the httpx.Auth that puts them on the wire.

`McpOAuth` hides tokens, endpoints, client secrets and refresh serialization
behind a few methods; nothing outside this module and McpAuthStore ever holds
a token. `McpBearerAuth` is handed to the MCP streamable HTTP client as
`auth=` and asks the service for a token on every request, so a refresh done
by another session or process is picked up without a reconnect.

Refresh race: two refreshers presenting the same rotating refresh token can
make an authorization server revoke the whole token family, so refreshes are
serialized BEFORE the network call: a per-connector asyncio.Lock for this
process, then McpAuthStore.try_lease (a compare-and-set on the grant version)
for other processes. The loser of the lease re-reads the row and adopts
whatever the winner committed.

The SDK's OAuthClientProvider is not used: it runs the whole consent flow
inside a single auth_flow with a blocking callback, which cannot span the two
browser round-trips this flow needs. Its wire models are reused for parsing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import weakref
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from claw.auth.mcp_oauth_discovery import (
    MAX_TOKEN_RESPONSE_BYTES,
    REAUTH_MARKER,
    TOKEN_TIMEOUT_S,
    AdminClientRef,
    BearerChallenge,
    ClientCredentials,
    ClientRef,
    FailureReason,
    Grant,
    MetadataDocClientRef,
    RegisteredClientRef,
    canonical_resource,
    parse_bearer_challenge,
)
from claw.db.stores import Lease, McpAuthStore
from claw.security.ssrf import ResponseTooLarge, pinned_client, read_bounded

_LEASE_POLL_SECONDS = 0.1
_LEASE_POLL_ATTEMPTS = 20
_PERMANENT_TOKEN_ERRORS = {
    "invalid_grant": FailureReason.INVALID_GRANT,
    "invalid_client": FailureReason.INVALID_CLIENT,
    "unauthorized_client": FailureReason.INVALID_CLIENT,
}


class McpAuthorizationNeeded(Exception):
    """The server challenged for a Bearer token and this connector has no
    usable grant (none at all, or one bound to a different resource)."""

    status = "needs_authorization"

    def __init__(self, challenge: BearerChallenge) -> None:
        super().__init__("This server requires authorization. Connect this account.")
        self.challenge = challenge


class McpReauthorizationRequired(Exception):
    """The grant is dead; only a new consent flow can revive it. str() carries
    REAUTH_MARKER and the reason code, never token material."""

    status = "reauthorization_required"

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(f"{REAUTH_MARKER}: {reason.value}")
        self.reason = reason


class McpTokenRefreshFailed(Exception):
    """A transient token-endpoint failure; the grant is intact and the lease
    released, so the next request retries."""


class McpOAuth:
    def __init__(
        self,
        store: McpAuthStore,
        *,
        http: Callable[..., httpx.AsyncClient] | None = None,
        now: Callable[[], datetime] | None = None,
        lease_poll_seconds: float = _LEASE_POLL_SECONDS,
    ) -> None:
        self._store = store
        self._http = http or (lambda: pinned_client(require_https=True, timeout=TOKEN_TIMEOUT_S))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lease_poll_seconds = lease_poll_seconds
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def http_auth(self, connector: Any) -> McpBearerAuth:
        """The auth for one connection to a per-user http MCP connector.
        Returned even when no grant exists: it is also how the Bearer
        challenge is observed, which is what reports needs_authorization."""
        assert connector.owner_id is not None, "OAuth is per-user only"
        return McpBearerAuth(self, connector)

    async def access_token(self, connector: Any, *, stale: str | None = None) -> str | None:
        """A token to send to `connector`, refreshing when needed.

        None means there is nothing to send: no grant, or a grant bound to a
        resource other than the connector's current URL (a retargeted URL
        never receives the old server's token). `stale` is a token the server
        just rejected; it is never returned again, so a 401 on a token we
        believed fresh forces a refresh (or adopts a peer's newer token).
        Raises McpReauthorizationRequired when the grant is failed."""
        loaded = await self._store.load(connector.id)
        if loaded is None:
            return None
        if loaded.failure is not None:
            raise McpReauthorizationRequired(loaded.failure)
        grant = loaded.grant
        if grant is None:
            return None
        if not connector.url.lower().startswith("https://"):
            return None
        if grant.resource != canonical_resource(connector.url):
            return None
        if grant.access_token != stale and grant.is_fresh(self._now()):
            return grant.access_token
        return await self._refresh(connector.id, stale)

    async def _refresh(self, connector_id: str, stale: str | None) -> str:
        lock = self._locks.get(connector_id)
        if lock is None:
            lock = self._locks[connector_id] = asyncio.Lock()
        async with lock:
            for attempt in range(_LEASE_POLL_ATTEMPTS):
                loaded = await self._store.load(connector_id)
                if loaded is None or loaded.grant is None:
                    raise McpTokenRefreshFailed("grant disappeared during refresh")
                if loaded.failure is not None:
                    raise McpReauthorizationRequired(loaded.failure)
                grant = loaded.grant
                now = self._now()
                if grant.access_token != stale and grant.is_fresh(now):
                    return grant.access_token
                if grant.refresh_token is None:
                    await self._store.mark_failed(connector_id, loaded.version, FailureReason.REJECTED_TOKEN)
                    raise McpReauthorizationRequired(FailureReason.REJECTED_TOKEN)
                lease = await self._store.try_lease(connector_id, loaded.version, now)
                if lease is None:
                    await asyncio.sleep(min(self._lease_poll_seconds * 2**attempt, 2.0))
                    continue
                return await self._refresh_under_lease(lease, grant)
        raise McpTokenRefreshFailed("refresh lease held elsewhere for too long")

    async def _refresh_under_lease(self, lease: Lease, grant: Grant) -> str:
        try:
            credentials = await self._credentials(grant.client)
        except McpReauthorizationRequired as exc:
            await self._store.fail_under_lease(lease, exc.reason)
            raise
        form = {
            "grant_type": "refresh_token",
            "refresh_token": grant.refresh_token,
            "resource": grant.resource,
        }
        try:
            token = await self._token_request(grant.token_endpoint, form, credentials)
        except _TokenEndpointRejected as exc:
            reason = _PERMANENT_TOKEN_ERRORS.get(exc.code)
            if reason is None:
                await self._store.release_lease(lease)
                raise McpTokenRefreshFailed(f"token endpoint rejected the refresh ({exc.code})") from exc
            await self._store.fail_under_lease(lease, reason)
            raise McpReauthorizationRequired(reason) from exc
        except BaseException:
            await self._store.release_lease(lease)
            raise
        now = self._now()
        refreshed = replace(
            grant,
            access_token=token.access_token,
            refresh_token=token.refresh_token or grant.refresh_token,
            expires_at=now + timedelta(seconds=token.expires_in) if token.expires_in is not None else None,
            scope=token.scope or grant.scope,
        )
        if not await self._store.commit_under_lease(lease, refreshed):
            # Only reachable if the lease expired mid-call, which
            # TOKEN_TIMEOUT_S < LEASE_TTL rules out; the rotated token is now
            # unknown to anyone, so the grant is gone.
            raise McpReauthorizationRequired(FailureReason.REJECTED_TOKEN)
        return refreshed.access_token

    async def _credentials(self, ref: ClientRef) -> ClientCredentials:
        if isinstance(ref, MetadataDocClientRef):
            return ClientCredentials(client_id=ref.client_id, client_secret=None, auth_method="none")
        assert isinstance(ref, (AdminClientRef, RegisteredClientRef))
        credentials = await self._store.client_credentials(ref.row_id)
        if credentials is None:
            raise McpReauthorizationRequired(FailureReason.CLIENT_GONE)
        return credentials

    async def _token_request(
        self, endpoint: str, form: dict[str, str], credentials: ClientCredentials
    ) -> OAuthToken:
        headers = {"Accept": "application/json"}
        data = dict(form)
        if credentials.auth_method == "client_secret_basic" and credentials.client_secret is not None:
            pair = f"{_form_quote(credentials.client_id)}:{_form_quote(credentials.client_secret)}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
        else:
            data["client_id"] = credentials.client_id
            if credentials.auth_method == "client_secret_post" and credentials.client_secret is not None:
                data["client_secret"] = credentials.client_secret
        async with self._http() as http:
            try:
                async with http.stream("POST", endpoint, data=data, headers=headers) as response:
                    body = await read_bounded(response, MAX_TOKEN_RESPONSE_BYTES)
            except ResponseTooLarge as exc:
                raise McpTokenRefreshFailed("token response too large") from exc
        if response.status_code in (400, 401):
            raise _TokenEndpointRejected(_error_code(body))
        if response.status_code != 200:
            raise McpTokenRefreshFailed(f"token endpoint answered {response.status_code}")
        try:
            return OAuthToken.model_validate_json(body)
        except ValidationError as exc:
            raise McpTokenRefreshFailed("token endpoint returned an unusable token response") from exc


class _TokenEndpointRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _error_code(body: bytes) -> str:
    try:
        code = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return "unparseable"
    return str(code) if code else "unspecified"


def _form_quote(value: str) -> str:
    # RFC 6749 §2.3.1: client id and secret are form-url-encoded before the
    # HTTP Basic header is built.
    return quote(value, safe="")


class McpBearerAuth(httpx.Auth):
    """Per-connection auth for the MCP streamable HTTP client.

    Holds no token of its own. `outcome` records what this connection learned
    about authorization (a challenge with no grant, or a dead grant) so
    ConnectorManager._connect can report a status instead of an error, since
    the SDK may surface the underlying failure as any exception type."""

    requires_response_body = False

    def __init__(self, oauth: McpOAuth, connector: Any) -> None:
        self._oauth = oauth
        self._connector = connector
        self.outcome: McpAuthorizationNeeded | McpReauthorizationRequired | None = None

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._token()
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401:
            return
        challenge = parse_bearer_challenge(response)
        if challenge is None:
            return
        if token is None:
            self.outcome = McpAuthorizationNeeded(challenge)
            return
        loaded = await self._oauth._store.load(self._connector.id)
        if challenge.error == "insufficient_scope":
            await self._fail(loaded, FailureReason.INSUFFICIENT_SCOPE)
        fresh = await self._token(stale=token)
        if fresh is None:
            return
        request.headers["Authorization"] = f"Bearer {fresh}"
        response = yield request
        if response.status_code == 401:
            await self._fail(await self._oauth._store.load(self._connector.id), FailureReason.REJECTED_TOKEN)

    async def _token(self, *, stale: str | None = None) -> str | None:
        try:
            return await self._oauth.access_token(self._connector, stale=stale)
        except McpReauthorizationRequired as exc:
            self.outcome = exc
            raise

    async def _fail(self, loaded: Any, reason: FailureReason) -> None:
        if loaded is not None:
            await self._oauth._store.mark_failed(self._connector.id, loaded.version, reason)
        self.outcome = McpReauthorizationRequired(reason)
        raise self.outcome
