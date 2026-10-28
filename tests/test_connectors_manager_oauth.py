"""ConnectorManager (claw/core/connectors.py) with McpOAuth wired in: a Bearer
challenge during the handshake becomes a connector status rather than an
error, that status never enters the error backoff, admin-global connectors
never carry a token, and OAuth material never survives redaction."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from claw.auth.mcp_oauth import McpOAuth
from claw.auth.mcp_oauth_discovery import FailureReason, Grant, MetadataDocClientRef
from claw.core.connectors import ConnectorManager, _redact_secrets, _requires_oauth_reauthorization
from claw.db.stores import ConnectorStore, McpAuthStore, UserStore
from claw.security.crypto import SecretBox
from claw.tools.registry import ToolRegistry

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
URL = "https://mcp.example.com/mcp"


async def _resolves_public(url: str) -> list[str]:
    return ["93.184.216.34"]


class ChallengingServer:
    """An MCP server that answers every unauthenticated POST with a Bearer
    challenge and accepts the tokens in `valid`."""

    def __init__(self, *valid: str) -> None:
        self.valid = set(valid)
        self.seen: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Authorization")
        self.seen.append(header)
        if header is not None and header.removeprefix("Bearer ") in self.valid:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://x/.well-known/oauth-protected-resource"'
            },
        )

    def streamablehttp_client(self):
        """Stands in for the SDK's client: runs the handshake POST through the
        given auth and, like the SDK's _handle_post_request, raises on a
        non-2xx instead of yielding streams."""
        attempts = []

        @asynccontextmanager
        async def fake(url, headers=None, auth=None, **_):
            attempts.append(url)
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler), auth=auth) as client:
                async with client.stream("POST", url, json={"method": "initialize"}) as response:
                    response.raise_for_status()
            raise AssertionError("handshake succeeded; this fake has no session to offer")
            yield  # pragma: no cover - makes this an async generator for asynccontextmanager

        fake.attempts = attempts
        return fake


def _grant(**overrides) -> Grant:
    fields = dict(
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=NOW + timedelta(hours=1),
        scope=None,
        issuer="https://as.example.com",
        resource=URL,
        token_endpoint="https://as.example.com/token",
        revocation_endpoint=None,
        client=MetadataDocClientRef(client_id="https://claw.example.com/client-metadata.json"),
    )
    fields.update(overrides)
    return Grant(**fields)


async def _world(db_factory, monkeypatch, server: ChallengingServer, *, owner: bool = True):
    users = UserStore(db_factory)
    user = await users.create(email="oauth@x.io", password_hash="h")
    box = SecretBox("test-secret")
    connectors = ConnectorStore(db_factory, secret_box=box)
    connector = await connectors.upsert(
        user.id if owner else None, "remote", transport="http", url=URL, enabled=True
    )
    auth_store = McpAuthStore(db_factory, box)
    oauth = McpOAuth(auth_store, http=lambda: httpx.AsyncClient(), now=lambda: NOW)
    mgr = ConnectorManager(connectors, connect_timeout_seconds=5, mcp_oauth=oauth)
    fake = server.streamablehttp_client()
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", fake)
    monkeypatch.setattr("claw.core.connectors.resolve_public_ips", _resolves_public)
    return SimpleNamespace(user=user, connector=connector, auth_store=auth_store, mgr=mgr, fake=fake)


async def test_a_challenge_without_a_grant_is_needs_authorization_not_an_error(db_factory, monkeypatch):
    server = ChallengingServer()
    w = await _world(db_factory, monkeypatch, server)
    registry = ToolRegistry()

    await w.mgr.sync_tools(w.user.id, registry)

    status = (await w.mgr.status(w.user.id))["remote"]
    assert status["status"] == "needs_authorization"
    assert "authorization" in status["error"].lower()
    assert server.seen == [None]

    await w.mgr.sync_tools(w.user.id, registry)
    state = w.mgr._users[w.user.id]
    assert state.error_streak == 0
    assert state.errored_monotonic is None
    assert len(w.fake.attempts) == 1


async def test_a_failed_grant_is_reauthorization_required_without_dialling(db_factory, monkeypatch):
    server = ChallengingServer("at-1")
    w = await _world(db_factory, monkeypatch, server)
    await w.auth_store.put_grant(w.connector.id, w.user.id, _grant())
    await w.auth_store.mark_failed(w.connector.id, 1, FailureReason.INVALID_GRANT)

    await w.mgr.sync_tools(w.user.id, ToolRegistry())

    status = (await w.mgr.status(w.user.id))["remote"]
    assert status["status"] == "reauthorization_required"
    assert server.seen == []
    assert w.mgr._users[w.user.id].error_streak == 0


async def test_a_grant_puts_the_token_on_the_handshake(db_factory, monkeypatch):
    server = ChallengingServer("at-1")
    w = await _world(db_factory, monkeypatch, server)
    await w.auth_store.put_grant(w.connector.id, w.user.id, _grant())

    await w.mgr.sync_tools(w.user.id, ToolRegistry())

    assert server.seen == ["Bearer at-1"]
    # The fake has no session to hand back, so the connect itself ends in a
    # plain error; the point is the Authorization header above.
    assert (await w.mgr.status(w.user.id))["remote"]["status"] == "error"


async def test_an_admin_global_connector_never_gets_oauth(db_factory, monkeypatch):
    server = ChallengingServer()
    w = await _world(db_factory, monkeypatch, server, owner=False)

    await w.mgr.sync_global()

    status = (await w.mgr.status_global())["remote"]
    assert status["status"] == "error"
    assert server.seen == [None]


def test_reauth_marker_is_recognised_by_the_tool_call_path():
    assert _requires_oauth_reauthorization("McpError: mcp_oauth_reauthorization_required: invalid_grant")


def test_redaction_scrubs_bearer_tokens_and_token_parameters():
    connector = SimpleNamespace(env={}, operations=None)
    text = (
        "401 for url https://as.example.com/token?code=AUTHCODE&state=s "
        "headers Authorization: Bearer eyJhbGci.payload.sig "
        "body grant_type=refresh_token&refresh_token=rt-SECRET&client_secret=cs-SECRET "
        'json {"access_token": "at-SECRET", "refresh_token": "rt-SECRET2", "scope": "x"} '
        "status code=401"
    )

    redacted = _redact_secrets(text, connector)

    for secret in ("AUTHCODE", "eyJhbGci.payload.sig", "rt-SECRET", "cs-SECRET", "at-SECRET", "rt-SECRET2"):
        assert secret not in redacted
    assert "state=s" in redacted
    assert '"scope": "x"' in redacted
    assert "status code=401" in redacted
