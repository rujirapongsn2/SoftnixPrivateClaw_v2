"""OAuth for per-user remote MCP connectors: the service that owns grants and
the httpx.Auth that puts them on the wire.

`McpOAuth` hides tokens, endpoints, client secrets and refresh serialization
behind a few methods; nothing outside this module and McpAuthStore ever holds
a token. `McpBearerAuth` is handed to the MCP streamable HTTP client as
`auth=` and asks the service for a token on every request, so a refresh done
by another session or process is picked up without a reconnect: at once in
this process, and within the grant cache's TTL (or on the next 401) in
another.

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
import hashlib
import hmac
import json
import secrets
import time
import weakref
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from loguru import logger
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError

from claw.auth.mcp_oauth_discovery import (
    CALLBACK_PATH,
    CLIENT_METADATA_PATH,
    MAX_TOKEN_RESPONSE_BYTES,
    PENDING_TTL,
    REAUTH_MARKER,
    REGISTRABLE_AUTH_METHODS,
    TOKEN_TIMEOUT_S,
    AdminClientRef,
    AuthView,
    BearerChallenge,
    CallbackOutcome,
    ClientCredentials,
    ClientRef,
    ClientStrategy,
    Discovered,
    DiscoveryError,
    FailureReason,
    Grant,
    MetadataDocClientRef,
    PendingFlow,
    RegisteredClientRef,
    ReturnTo,
    auth_view,
    canonical_resource,
    choose_client_strategy,
    discover,
    parse_bearer_challenge,
    registration_auth_method,
)
from claw.db.stores import Lease, LoadedAuth, McpAuthStore
from claw.security.ssrf import ResponseTooLarge, UnsafeUrlError, pinned_client, read_bounded

_LEASE_POLL_SECONDS = 0.1
_LEASE_POLL_ATTEMPTS = 20
# How long this process trusts its copy of a grant row. Every write here
# invalidates it at once; the TTL only bounds how long another process's
# refresh or disconnect can go unseen.
_GRANT_CACHE_TTL_S = 30.0
CLIENT_NAME = "Softnix PrivateClaw"
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


class McpOAuthStartError(Exception):
    """A consent flow could not be started. `code` is stable for the UI;
    the message is safe to show the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Started:
    authorize_url: str
    # Set by the route as an HttpOnly cookie; only its hash is stored, and
    # the callback must present it, so a victim's browser cannot complete a
    # flow an attacker started.
    browser_nonce: str


@dataclass(frozen=True)
class Completed:
    outcome: CallbackOutcome
    connector_id: str | None
    owner_id: str | None
    return_to: ReturnTo


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class McpOAuth:
    def __init__(
        self,
        store: McpAuthStore,
        *,
        public_base_url: str,
        http: Callable[..., httpx.AsyncClient] | None = None,
        now: Callable[[], datetime] | None = None,
        lease_poll_seconds: float = _LEASE_POLL_SECONDS,
    ) -> None:
        self._store = store
        # One fixed redirect URI for every mount (claw and sbot): the flow's
        # origin travels in the pending row, not the URL.
        self.redirect_uri = public_base_url.rstrip("/") + CALLBACK_PATH
        self._public_base_url = public_base_url
        # A Client ID Metadata Document's client_id is the URL it is served at.
        self.client_metadata_url = public_base_url.rstrip("/") + CLIENT_METADATA_PATH
        self._http = http or (lambda: pinned_client(require_https=True, timeout=TOKEN_TIMEOUT_S))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lease_poll_seconds = lease_poll_seconds
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # connector id -> (monotonic deadline, row); a None row is cached too,
        # since most http connectors never have one and ask on every request.
        self._grants: dict[str, tuple[float, LoadedAuth | None]] = {}
        self._grants_generation = 0
        self._monotonic = time.monotonic

    @property
    def store(self) -> McpAuthStore:
        return self._store

    async def begin(self, user_id: str, connector_id: str, *, return_to: ReturnTo) -> Started:
        """Start a consent flow; the caller sends the browser to
        `authorize_url`. Raises LookupError unless `connector_id` is an http
        MCP connector `user_id` owns, McpOAuthStartError otherwise."""
        url = await self._store.oauth_connector_url(user_id, connector_id)
        if url is None:
            raise LookupError("connector not found")
        if not url.lower().startswith("https://"):
            raise McpOAuthStartError("https_required", "OAuth is only available for HTTPS MCP servers.")
        try:
            async with self._http() as http:
                found = await discover(http, url)
        except DiscoveryError as exc:
            raise McpOAuthStartError("discovery_failed", str(exc)) from exc
        scope = await self._scope_to_request(connector_id, found)

        admin_row = await self._store.admin_client_id(found.issuer, self.redirect_uri)
        strategy = choose_client_strategy(
            has_admin_client=admin_row is not None,
            public_base_url=self._public_base_url,
            cimd_supported=found.cimd_supported,
            registration_endpoint=found.registration_endpoint,
            token_auth_methods=found.token_auth_methods,
        )
        client: ClientRef
        match strategy:
            case ClientStrategy.ADMIN:
                assert admin_row is not None
                client = AdminClientRef(row_id=admin_row)
            case ClientStrategy.METADATA_DOCUMENT:
                client = MetadataDocClientRef(client_id=self.client_metadata_url)
            case ClientStrategy.REGISTRATION:
                client = await self._registered_client(found, scope)
            case None:
                raise McpOAuthStartError(
                    "no_client",
                    f"{found.issuer} does not let this app register itself. "
                    "Ask your administrator to register an OAuth client for it.",
                )
        try:
            credentials = await self._credentials(client)
        except McpReauthorizationRequired as exc:
            raise McpOAuthStartError(
                "no_client", "The OAuth client for this server was just removed."
            ) from exc

        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        pending = PendingFlow(
            code_verifier=pkce.code_verifier,
            browser_nonce_hash=_sha256(nonce),
            issuer=found.issuer,
            resource=found.resource,
            token_endpoint=found.token_endpoint,
            revocation_endpoint=found.revocation_endpoint,
            client=client,
            redirect_uri=self.redirect_uri,
            scope=scope,
            return_to=return_to,
        )
        await self._store.put_pending(
            connector_id, user_id, _sha256(state), self._now() + PENDING_TTL, pending
        )
        params = {
            "response_type": "code",
            "client_id": credentials.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": found.resource,
        }
        if scope:
            params["scope"] = scope
        authorize_url = httpx.URL(found.authorization_endpoint).copy_merge_params(params)
        return Started(authorize_url=str(authorize_url), browser_nonce=nonce)

    async def _scope_to_request(self, connector_id: str, found: Discovered) -> str | None:
        """The discovered scope, widened by what the connector's grant at this
        same server holds and what a step-up challenge asked for, so a
        Reconnect never comes back with less than the server demanded."""
        loaded = await self._store.load(connector_id)
        grant = loaded.grant if loaded is not None else None
        if grant is None or grant.issuer != found.issuer or grant.resource != found.resource:
            return found.scope
        return _union_scopes(found.scope, grant.scope, grant.required_scope)

    def client_metadata_document(self) -> dict:
        """The Client ID Metadata Document served at `client_metadata_url`."""
        return {
            **self._client_metadata("none"),
            "client_id": self.client_metadata_url,
        }

    def _client_metadata(self, auth_method: str) -> dict:
        return {
            "client_name": CLIENT_NAME,
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": auth_method,
        }

    async def _registered_client(self, found: Discovered, scope: str | None) -> RegisteredClientRef:
        """The cached Dynamic Client Registration for `found.issuer`,
        registering (RFC 7591) when there is none or its secret expired. Only
        begin() reaches this, so registrations are bounded by users clicking
        Connect; a refresh or a Bearer challenge never registers."""
        cached = await self._store.registered_client(found.issuer, self.redirect_uri)
        if cached is not None:
            if cached.secret_expires_at is None or cached.secret_expires_at > self._now():
                return RegisteredClientRef(row_id=cached.row_id)
            await self._store.delete_registered_client(cached.row_id)

        endpoint = found.registration_endpoint
        auth_method = registration_auth_method(found.token_auth_methods)
        assert endpoint is not None and auth_method is not None, "choose_client_strategy checked both"
        host = urlsplit(endpoint).hostname or endpoint
        failed = f"Could not register this app with {host}."
        if urlsplit(endpoint).scheme.lower() != "https":
            raise McpOAuthStartError("registration_failed", f"The registration endpoint {host} is not HTTPS.")
        body = self._client_metadata(auth_method)
        if scope:
            body["scope"] = scope
        try:
            async with self._http() as http:
                async with http.stream(
                    "POST", endpoint, json=body, headers={"Accept": "application/json"}
                ) as response:
                    raw = await read_bounded(response, MAX_TOKEN_RESPONSE_BYTES)
        except UnsafeUrlError as exc:
            raise McpOAuthStartError(
                "registration_failed", f"The registration endpoint {host} is not a public HTTPS address."
            ) from exc
        except (httpx.HTTPError, ResponseTooLarge) as exc:
            logger.warning("MCP OAuth registration at {} failed: {}", host, type(exc).__name__)
            raise McpOAuthStartError("registration_failed", failed) from exc
        if response.status_code not in (200, 201):
            logger.warning("MCP OAuth registration at {} answered {}", host, response.status_code)
            raise McpOAuthStartError("registration_failed", failed)
        try:
            info = OAuthClientInformationFull.model_validate_json(raw)
        except ValidationError as exc:
            raise McpOAuthStartError("registration_failed", failed) from exc
        # The server may assign a different method than we asked for; it is
        # the one the client must use from now on.
        method = info.token_endpoint_auth_method or auth_method
        if (
            not info.client_id
            or method not in REGISTRABLE_AUTH_METHODS
            or (method != "none" and not info.client_secret)
        ):
            raise McpOAuthStartError("registration_failed", failed)
        row_id = await self._store.add_registered_client(
            issuer=found.issuer,
            redirect_uri=self.redirect_uri,
            client_id=info.client_id,
            client_secret=info.client_secret if method != "none" else None,
            auth_method=method,
            registration=info.model_dump(mode="json", exclude_none=True),
        )
        return RegisteredClientRef(row_id=row_id)

    async def complete(
        self, *, state: str, code: str | None, error: str | None, browser_nonce: str | None
    ) -> Completed:
        """Finish the flow the authorization server redirected back. Never
        raises: every failure is a CallbackOutcome for the browser."""
        consumed = await self._store.consume_pending(_sha256(state), self._now()) if state else None
        if consumed is None:
            return Completed(CallbackOutcome.EXPIRED, None, None, ReturnTo.CLAW)
        connector_id, owner_id, pending = consumed

        def done(outcome: CallbackOutcome) -> Completed:
            return Completed(outcome, connector_id, owner_id, pending.return_to)

        # Checked after consuming, so a wrong cookie burns the flow instead of
        # leaving it open to further guesses.
        if not hmac.compare_digest(_sha256(browser_nonce or ""), pending.browser_nonce_hash):
            return done(CallbackOutcome.BROWSER_MISMATCH)
        if error == "access_denied":
            return done(CallbackOutcome.DENIED)
        if error or not code:
            return done(CallbackOutcome.ERROR)

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.code_verifier,
            "resource": pending.resource,
        }
        try:
            token = await self._token_request(
                pending.token_endpoint, form, await self._credentials(pending.client)
            )
        except _TokenEndpointRejected as exc:
            logger.warning("MCP OAuth code exchange rejected for connector {}: {}", connector_id, exc.code)
            await self._forget_rejected_client(pending.client, exc.code)
            return done(CallbackOutcome.TOKEN_FAILED)
        except (McpTokenRefreshFailed, McpReauthorizationRequired, httpx.HTTPError, UnsafeUrlError) as exc:
            logger.warning(
                "MCP OAuth code exchange failed for connector {}: {}", connector_id, type(exc).__name__
            )
            return done(CallbackOutcome.TOKEN_FAILED)

        now = self._now()
        grant = Grant(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=now + timedelta(seconds=token.expires_in) if token.expires_in is not None else None,
            scope=token.scope or pending.scope,
            issuer=pending.issuer,
            resource=pending.resource,
            token_endpoint=pending.token_endpoint,
            revocation_endpoint=pending.revocation_endpoint,
            client=pending.client,
        )
        try:
            await self._store.put_grant(connector_id, owner_id, grant)
        except LookupError:
            # The connector was deleted or changed kind while consent was open.
            return done(CallbackOutcome.ERROR)
        finally:
            self.forget(connector_id)
        return done(CallbackOutcome.CONNECTED)

    async def disconnect(self, user_id: str, connector_id: str, *, revoke: bool) -> bool:
        """Forget the grant, then, when asked, revoke it at the authorization
        server (RFC 7009). The local delete comes first and is unconditional,
        so a failing server can never keep alive a grant the user dropped.
        Returns whether the server accepted every revocation. Raises
        LookupError unless `connector_id` is an http MCP connector `user_id`
        owns; idempotent otherwise."""
        if await self._store.oauth_connector_url(user_id, connector_id) is None:
            raise LookupError("connector not found")
        grant = await self._store.take_grant(user_id, connector_id)
        self.forget(connector_id)
        if not revoke or grant is None or grant.revocation_endpoint is None:
            return False
        try:
            credentials = await self._credentials(grant.client)
        except McpReauthorizationRequired:
            return False
        tokens = [("refresh_token", grant.refresh_token), ("access_token", grant.access_token)]
        revoked = True
        for hint, value in tokens:
            if value is None:
                continue
            form = {"token": value, "token_type_hint": hint}
            data, headers = _client_auth(form, credentials)
            try:
                async with self._http() as http:
                    async with http.stream(
                        "POST", grant.revocation_endpoint, data=data, headers=headers
                    ) as response:
                        await read_bounded(response, MAX_TOKEN_RESPONSE_BYTES)
                ok = response.status_code == 200
            except (httpx.HTTPError, UnsafeUrlError, ResponseTooLarge) as exc:
                logger.warning(
                    "MCP OAuth revocation failed for connector {}: {}", connector_id, type(exc).__name__
                )
                ok = False
            revoked = revoked and ok
        return revoked

    async def describe(self, owner_id: str, urls: Mapping[str, str]) -> dict[str, AuthView]:
        """AuthViews for connectors `owner_id` owns, keyed by id, from one
        query; `urls` maps each connector id to its current URL. A grant bound
        to a URL the connector no longer points at is reported as absent,
        matching access_token, which will not send it."""
        loaded = await self._store.load_many(owner_id, list(urls))
        views = {}
        for cid, row in loaded.items():
            grant = row.grant
            if grant is not None and grant.resource != canonical_resource(urls[cid]):
                grant = None
            views[cid] = auth_view(grant, row.failure)
        return views

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
        loaded = await (self._store.load(connector.id) if stale is not None else self._cached(connector.id))
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
        try:
            return await self._refresh(connector.id, stale)
        finally:
            # Rotated, failed, or adopted from a peer: either way the cached
            # row is no longer the one to trust.
            self.forget(connector.id)

    async def _cached(self, connector_id: str) -> LoadedAuth | None:
        hit = self._grants.get(connector_id)
        if hit is not None and hit[0] > self._monotonic():
            return hit[1]
        generation = self._grants_generation
        loaded = await self._store.load(connector_id)
        # A write that landed while this read was in flight wins over it.
        if generation == self._grants_generation:
            self._grants[connector_id] = (self._monotonic() + _GRANT_CACHE_TTL_S, loaded)
        return loaded

    def forget(self, connector_id: str) -> None:
        """Drop this process's copy of the connector's grant row; called
        after every write to it, including the connector's deletion."""
        self._grants_generation += 1
        self._grants.pop(connector_id, None)

    async def mark_rejected(
        self, connector: Any, reason: FailureReason, *, token: str, scope: str | None = None
    ) -> None:
        """Fail the grant because `connector`'s server refused `token`, but
        only while `token` is still the grant's access token: a refusal of a
        token another session has since replaced says nothing about the new
        one. `scope` is what a step-up challenge asked for; the next begin()
        requests it."""
        loaded = await self._store.load(connector.id)
        if loaded is None or loaded.grant is None or loaded.grant.access_token != token:
            return
        grant = replace(loaded.grant, required_scope=scope) if scope else None
        await self._store.mark_failed(connector.id, loaded.version, reason, grant=grant)
        self.forget(connector.id)

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
            await self._forget_rejected_client(grant.client, exc.code)
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

    async def _forget_rejected_client(self, client: ClientRef, error_code: str) -> None:
        """Drop a cached registration the server answered invalid_client for,
        so the next Connect registers afresh instead of reusing a dead client.
        Admin clients are the administrator's to fix."""
        if error_code == "invalid_client" and isinstance(client, RegisteredClientRef):
            await self._store.delete_registered_client(client.row_id)

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
        data, headers = _client_auth(form, credentials)
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


def _union_scopes(*scopes: str | None) -> str | None:
    merged = dict.fromkeys(item for scope in scopes if scope for item in scope.split())
    return " ".join(merged) or None


def _client_auth(
    form: dict[str, str], credentials: ClientCredentials
) -> tuple[dict[str, str], dict[str, str]]:
    """The form and headers for a token or revocation request, with client
    authentication applied per the client's token_endpoint_auth_method."""
    headers = {"Accept": "application/json"}
    data = dict(form)
    if credentials.auth_method == "client_secret_basic" and credentials.client_secret is not None:
        pair = f"{_form_quote(credentials.client_id)}:{_form_quote(credentials.client_secret)}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    else:
        data["client_id"] = credentials.client_id
        if credentials.auth_method == "client_secret_post" and credentials.client_secret is not None:
            data["client_secret"] = credentials.client_secret
    return data, headers


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
        challenge = parse_bearer_challenge(response)
        if challenge is None:
            return
        step_up = challenge.error == "insufficient_scope"
        if response.status_code == 403 and not step_up:
            return
        if token is None:
            self.outcome = McpAuthorizationNeeded(challenge)
            return
        if step_up:
            # A refresh cannot widen a grant's scope; only a new consent can.
            await self._reject(token, FailureReason.INSUFFICIENT_SCOPE, scope=challenge.scope)
        fresh = await self._token(stale=token)
        if fresh is None:
            return
        request.headers["Authorization"] = f"Bearer {fresh}"
        response = yield request
        if response.status_code == 401:
            await self._reject(fresh, FailureReason.REJECTED_TOKEN)

    async def _token(self, *, stale: str | None = None) -> str | None:
        try:
            return await self._oauth.access_token(self._connector, stale=stale)
        except McpReauthorizationRequired as exc:
            self.outcome = exc
            raise

    async def _reject(self, token: str, reason: FailureReason, *, scope: str | None = None) -> None:
        await self._oauth.mark_rejected(self._connector, reason, token=token, scope=scope)
        self.outcome = McpReauthorizationRequired(reason)
        raise self.outcome
