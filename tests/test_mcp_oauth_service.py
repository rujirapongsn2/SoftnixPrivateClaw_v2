"""McpOAuth and McpBearerAuth (claw/auth/mcp_oauth.py) against MockTransport
authorization and MCP servers: refresh serialization, rotation, failure
classification, and what goes on the wire."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claw.auth.mcp_oauth import (
    REAUTH_MARKER,
    McpAuthorizationNeeded,
    McpOAuth,
    McpReauthorizationRequired,
    McpTokenRefreshFailed,
)
from claw.auth.mcp_oauth_discovery import (
    LEASE_TTL,
    AdminClientRef,
    FailureReason,
    Grant,
    MetadataDocClientRef,
)
from claw.db.stores import ConnectorStore, McpAuthStore, UserStore
from claw.security.crypto import SecretBox

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
TOKEN_ENDPOINT = "https://as.example.com/token"


def _grant(**overrides) -> Grant:
    fields = dict(
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=NOW + timedelta(hours=1),
        scope="mcp:read",
        issuer="https://as.example.com",
        resource="https://mcp.example.com/mcp",
        token_endpoint=TOKEN_ENDPOINT,
        revocation_endpoint=None,
        client=MetadataDocClientRef(client_id="https://claw.example.com/api/mcp-oauth/client-metadata.json"),
    )
    fields.update(overrides)
    return Grant(**fields)


class TokenEndpoint:
    """A scripted token endpoint: `responses` are consumed in order, the last
    one repeating. Every request is recorded with its decoded form body."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses) or [self.issued("at-2", "rt-2")]
        self.requests: list[tuple[httpx.Request, dict[str, str]]] = []

    @staticmethod
    def issued(
        access_token: str, refresh_token: str | None = None, expires_in: int | None = 3600
    ) -> httpx.Response:
        body = {"access_token": access_token, "token_type": "bearer"}
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
        if expires_in is not None:
            body["expires_in"] = expires_in
        return httpx.Response(200, json=body)

    @staticmethod
    def rejected(code: str) -> httpx.Response:
        return httpx.Response(400, json={"error": code})

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        self.requests.append((request, form))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
async def world(db_factory):
    users = UserStore(db_factory)
    alice = await users.create(email="alice@x.io", password_hash="h")
    connectors = ConnectorStore(db_factory, secret_box=SecretBox("test-secret"))
    connector = await connectors.upsert(
        alice.id, "remote", transport="http", url="https://mcp.example.com/mcp"
    )
    store = McpAuthStore(db_factory, SecretBox("test-secret"))
    endpoint = TokenEndpoint()
    clock = {"now": NOW}
    oauth = McpOAuth(store, public_base_url="https://claw.example.com", http=endpoint.client, now=lambda: clock["now"], lease_poll_seconds=0.01)
    return {
        "alice": alice.id,
        "connector": connector,
        "store": store,
        "endpoint": endpoint,
        "oauth": oauth,
        "clock": clock,
    }


async def _seed(world, **overrides) -> None:
    await world["store"].put_grant(world["connector"].id, world["alice"], _grant(**overrides))


async def test_ten_concurrent_requests_on_an_expired_grant_make_one_token_call(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))

    tokens = await asyncio.gather(*(world["oauth"].access_token(world["connector"]) for _ in range(10)))

    assert tokens == ["at-2"] * 10
    assert len(world["endpoint"].requests) == 1
    request, form = world["endpoint"].requests[0]
    assert str(request.url) == TOKEN_ENDPOINT
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-1"
    assert form["resource"] == "https://mcp.example.com/mcp"
    assert form["client_id"] == "https://claw.example.com/api/mcp-oauth/client-metadata.json"


async def test_rotated_refresh_token_is_persisted_and_an_omitted_one_is_kept(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    world["endpoint"].responses = [TokenEndpoint.issued("at-2", "rt-2"), TokenEndpoint.issued("at-3")]

    assert await world["oauth"].access_token(world["connector"]) == "at-2"
    loaded = await world["store"].load(world["connector"].id)
    assert loaded.grant.refresh_token == "rt-2"
    assert loaded.grant.expires_at == NOW + timedelta(hours=1)
    assert loaded.version == 2

    world["clock"]["now"] = NOW + timedelta(hours=2)
    assert await world["oauth"].access_token(world["connector"]) == "at-3"
    loaded = await world["store"].load(world["connector"].id)
    assert loaded.grant.refresh_token == "rt-2"
    assert world["endpoint"].requests[1][1]["refresh_token"] == "rt-2"


async def test_a_token_believed_fresh_but_rejected_is_refreshed_not_resent(world):
    await _seed(world)

    assert await world["oauth"].access_token(world["connector"], stale="at-1") == "at-2"
    assert len(world["endpoint"].requests) == 1
    assert await world["oauth"].access_token(world["connector"], stale="at-1") == "at-2"
    assert len(world["endpoint"].requests) == 1


async def test_invalid_grant_marks_the_connector_for_reauthorization(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    world["endpoint"].responses = [TokenEndpoint.rejected("invalid_grant")]

    with pytest.raises(McpReauthorizationRequired) as raised:
        await world["oauth"].access_token(world["connector"])
    assert raised.value.reason is FailureReason.INVALID_GRANT
    assert REAUTH_MARKER in str(raised.value)
    assert "rt-1" not in str(raised.value) and "at-1" not in str(raised.value)

    with pytest.raises(McpReauthorizationRequired):
        await world["oauth"].access_token(world["connector"])
    assert len(world["endpoint"].requests) == 1
    assert (await world["store"].load(world["connector"].id)).failure is FailureReason.INVALID_GRANT


async def test_transient_token_error_leaves_the_grant_intact_and_releases_the_lease(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    world["endpoint"].responses = [httpx.Response(503), TokenEndpoint.issued("at-2", "rt-2")]

    with pytest.raises(McpTokenRefreshFailed):
        await world["oauth"].access_token(world["connector"])
    loaded = await world["store"].load(world["connector"].id)
    assert loaded.grant.refresh_token == "rt-1"
    assert loaded.failure is None
    assert loaded.version == 1

    assert await world["oauth"].access_token(world["connector"]) == "at-2"
    assert len(world["endpoint"].requests) == 2


async def test_a_lease_left_by_a_crashed_refresher_is_taken_over_once_expired(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    store = world["store"]
    abandoned = await store.try_lease(
        world["connector"].id, version=1, now=NOW - LEASE_TTL - timedelta(seconds=1)
    )
    assert abandoned is not None

    assert await world["oauth"].access_token(world["connector"]) == "at-2"
    assert len(world["endpoint"].requests) == 1
    assert await store.commit_under_lease(abandoned, _grant(access_token="zombie")) is False
    assert (await store.load(world["connector"].id)).grant.access_token == "at-2"


async def test_a_loser_adopts_the_lease_holders_token_without_calling_the_endpoint(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    store = world["store"]
    other_process = await store.try_lease(world["connector"].id, version=1, now=NOW)
    assert other_process is not None

    async def other_process_commits():
        await asyncio.sleep(0.03)
        assert await store.commit_under_lease(
            other_process, _grant(access_token="theirs", refresh_token="rt-2")
        )

    token, _ = await asyncio.gather(world["oauth"].access_token(world["connector"]), other_process_commits())

    assert token == "theirs"
    assert world["endpoint"].requests == []


async def test_a_stale_writer_cannot_overwrite_a_newer_grant(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    store = world["store"]
    stale = await store.try_lease(world["connector"].id, version=1, now=NOW)
    await store.release_lease(stale)

    assert await world["oauth"].access_token(world["connector"]) == "at-2"
    assert await store.commit_under_lease(stale, _grant(access_token="stale")) is False
    assert await store.try_lease(world["connector"].id, version=1, now=NOW + timedelta(minutes=5)) is None
    assert (await store.load(world["connector"].id)).grant.access_token == "at-2"


async def test_admin_client_credentials_go_in_the_basic_header(world):
    row_id = await world["store"].upsert_admin_client(
        issuer="https://as.example.com",
        client_id="my client",
        client_secret="s3cret:!",
        auth_method="client_secret_basic",
        redirect_uri="https://claw.example.com/api/mcp-oauth/callback",
    )
    await _seed(world, expires_at=NOW - timedelta(minutes=1), client=AdminClientRef(row_id=row_id))

    assert await world["oauth"].access_token(world["connector"]) == "at-2"
    request, form = world["endpoint"].requests[0]
    assert request.headers["Authorization"] == "Basic bXklMjBjbGllbnQ6czNjcmV0JTNBJTIx"
    assert "client_id" not in form and "client_secret" not in form


async def test_a_deleted_admin_client_fails_the_grant_as_client_gone(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1), client=AdminClientRef(row_id="gone"))

    with pytest.raises(McpReauthorizationRequired) as raised:
        await world["oauth"].access_token(world["connector"])
    assert raised.value.reason is FailureReason.CLIENT_GONE
    assert world["endpoint"].requests == []
    assert (await world["store"].load(world["connector"].id)).failure is FailureReason.CLIENT_GONE


async def test_no_grant_or_mismatched_resource_or_cleartext_url_yields_no_token(world):
    assert await world["oauth"].access_token(world["connector"]) is None

    await _seed(world, resource="https://other.example.com/mcp", expires_at=NOW - timedelta(minutes=1))
    assert await world["oauth"].access_token(world["connector"]) is None
    assert world["endpoint"].requests == []

    class Retargeted:
        id = world["connector"].id
        owner_id = world["alice"]
        url = "http://mcp.example.com/mcp"

    await _seed(world)
    assert await world["oauth"].access_token(Retargeted()) is None


# --- McpBearerAuth on the wire ------------------------------------------------


class McpServer:
    """Accepts exactly the tokens in `valid`; anything else gets a Bearer challenge."""

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
                "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                + (', error="invalid_token"' if header else "")
            },
        )

    def client(self, auth: httpx.Auth) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), auth=auth)


async def _post(server: McpServer, auth: httpx.Auth, url: str) -> httpx.Response:
    async with server.client(auth) as client:
        return await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})


async def test_header_is_attached_on_the_happy_path(world):
    await _seed(world)
    server = McpServer("at-1")
    auth = world["oauth"].http_auth(world["connector"])

    response = await _post(server, auth, world["connector"].url)

    assert response.status_code == 200
    assert server.seen == ["Bearer at-1"]
    assert auth.outcome is None


async def test_no_header_when_the_grant_is_bound_to_another_resource(world):
    await _seed(world, resource="https://other.example.com/mcp")
    server = McpServer("at-1")
    auth = world["oauth"].http_auth(world["connector"])

    response = await _post(server, auth, world["connector"].url)

    assert response.status_code == 401
    assert server.seen == [None]
    assert isinstance(auth.outcome, McpAuthorizationNeeded)
    assert world["endpoint"].requests == []


async def test_a_challenge_with_no_grant_records_needs_authorization(world):
    server = McpServer()
    auth = world["oauth"].http_auth(world["connector"])

    response = await _post(server, auth, world["connector"].url)

    assert response.status_code == 401
    assert server.seen == [None]
    assert isinstance(auth.outcome, McpAuthorizationNeeded)
    assert (
        auth.outcome.challenge.resource_metadata
        == "https://mcp.example.com/.well-known/oauth-protected-resource"
    )


async def test_a_rejected_fresh_token_is_refreshed_once_and_the_request_retried(world):
    await _seed(world)
    server = McpServer("at-2")
    auth = world["oauth"].http_auth(world["connector"])

    response = await _post(server, auth, world["connector"].url)

    assert response.status_code == 200
    assert server.seen == ["Bearer at-1", "Bearer at-2"]
    assert len(world["endpoint"].requests) == 1
    assert auth.outcome is None


async def test_a_token_rejected_even_after_refresh_fails_the_grant(world):
    await _seed(world)
    server = McpServer("something-else")
    auth = world["oauth"].http_auth(world["connector"])

    with pytest.raises(McpReauthorizationRequired) as raised:
        await _post(server, auth, world["connector"].url)

    assert raised.value.reason is FailureReason.REJECTED_TOKEN
    assert server.seen == ["Bearer at-1", "Bearer at-2"]
    assert auth.outcome is raised.value
    assert (await world["store"].load(world["connector"].id)).failure is FailureReason.REJECTED_TOKEN


async def test_a_failed_grant_sends_nothing_and_reports_reauthorization(world):
    await _seed(world)
    await world["store"].mark_failed(world["connector"].id, 1, FailureReason.INVALID_GRANT)
    server = McpServer("at-1")
    auth = world["oauth"].http_auth(world["connector"])

    with pytest.raises(McpReauthorizationRequired):
        await _post(server, auth, world["connector"].url)

    assert server.seen == []
    assert isinstance(auth.outcome, McpReauthorizationRequired)


async def test_http_auth_refuses_a_global_connector(world):
    class Shared:
        id = "shared"
        owner_id = None
        url = "https://mcp.example.com/mcp"

    with pytest.raises(AssertionError):
        world["oauth"].http_auth(Shared())


async def test_a_non_bearer_token_response_is_a_transient_failure(world):
    await _seed(world, expires_at=NOW - timedelta(minutes=1))
    world["endpoint"].responses = [httpx.Response(200, json={"access_token": "x", "token_type": "mac"})]

    with pytest.raises(McpTokenRefreshFailed):
        await world["oauth"].access_token(world["connector"])
    loaded = await world["store"].load(world["connector"].id)
    assert loaded.grant.access_token == "at-1" and loaded.failure is None
