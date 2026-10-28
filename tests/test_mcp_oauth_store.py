"""McpAuthStore (claw/db/stores.py) and the pure helpers in
claw/auth/mcp_oauth_discovery.py: the per-user gate, the encrypted grant, and
the lease/version compare-and-set that keeps two refreshers from presenting
the same rotating refresh token."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from claw.auth.mcp_oauth_discovery import (
    LEASE_TTL,
    AdminClientRef,
    FailureReason,
    Grant,
    MetadataDocClientRef,
    canonical_resource,
    decode_grant,
    encode_grant,
    parse_bearer_challenge,
)
from claw.db.stores import ConnectorStore, McpAuthStore, UserStore
from claw.security.crypto import SecretBox

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _grant(**overrides) -> Grant:
    fields = dict(
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=NOW + timedelta(hours=1),
        scope="mcp:read",
        issuer="https://as.example.com",
        resource="https://mcp.example.com/mcp",
        token_endpoint="https://as.example.com/token",
        revocation_endpoint=None,
        client=MetadataDocClientRef(client_id="https://claw.example.com/api/mcp-oauth/client-metadata.json"),
    )
    fields.update(overrides)
    return Grant(**fields)


@pytest.mark.parametrize(
    "url,resource",
    [
        ("https://MCP.Example.com/mcp", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:443/mcp/", "https://mcp.example.com/mcp"),
        ("https://mcp.example.com:8443/mcp?token=x#frag", "https://mcp.example.com:8443/mcp"),
        ("https://mcp.example.com", "https://mcp.example.com"),
        ("https://mcp.example.com/", "https://mcp.example.com"),
    ],
)
def test_canonical_resource(url, resource):
    assert canonical_resource(url) == resource


def test_parse_bearer_challenge_reads_the_fields_and_ignores_other_responses():
    challenged = httpx.Response(
        401,
        headers={
            "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", '
            'scope="files:read", error="invalid_token"'
        },
    )
    challenge = parse_bearer_challenge(challenged)
    assert challenge.resource_metadata == "https://mcp.example.com/.well-known/oauth-protected-resource"
    assert challenge.scope == "files:read"
    assert challenge.error == "invalid_token"
    assert parse_bearer_challenge(httpx.Response(401, headers={"WWW-Authenticate": "Basic realm=x"})) is None
    assert parse_bearer_challenge(httpx.Response(200)) is None
    assert parse_bearer_challenge(httpx.Response(401)) is None


def test_grant_codec_round_trips_every_client_kind_and_rejects_garbage():
    for client in (MetadataDocClientRef(client_id="https://x/y"), AdminClientRef(row_id="r1")):
        grant = _grant(client=client, expires_at=None)
        assert decode_grant(encode_grant(grant)) == grant
    assert decode_grant(encode_grant(_grant())) == _grant()
    with pytest.raises(ValueError):
        decode_grant('{"access_token": "x"}')
    with pytest.raises(ValueError):
        decode_grant("not json")


@pytest.fixture
async def world(db_factory):
    users = UserStore(db_factory)
    alice = await users.create(email="alice@x.io", password_hash="h")
    bob = await users.create(email="bob@x.io", password_hash="h")
    connectors = ConnectorStore(db_factory, secret_box=SecretBox("test-secret"))
    own = await connectors.upsert(alice.id, "own", transport="http", url="https://mcp.example.com/mcp")
    api = await connectors.upsert(
        alice.id, "api", kind="api", transport="http", url="https://api.example.com"
    )
    stdio = await connectors.upsert(alice.id, "local", transport="stdio", command="python -m x")
    shared = await connectors.upsert(None, "shared", transport="http", url="https://mcp.example.com/mcp")
    store = McpAuthStore(db_factory, SecretBox("test-secret"))
    return {
        "store": store,
        "alice": alice.id,
        "bob": bob.id,
        "own": own.id,
        "api": api.id,
        "stdio": stdio.id,
        "shared": shared.id,
    }


async def test_put_grant_only_for_an_http_mcp_connector_owned_by_the_user(world):
    store = world["store"]
    with pytest.raises(LookupError):
        await store.put_grant(world["shared"], world["alice"], _grant())
    with pytest.raises(LookupError):
        await store.put_grant(world["shared"], None, _grant())
    with pytest.raises(LookupError):
        await store.put_grant(world["own"], world["bob"], _grant())
    with pytest.raises(LookupError):
        await store.put_grant(world["api"], world["alice"], _grant())
    with pytest.raises(LookupError):
        await store.put_grant(world["stdio"], world["alice"], _grant())
    assert await store.load(world["shared"]) is None
    assert await store.load(world["own"]) is None

    await store.put_grant(world["own"], world["alice"], _grant())
    loaded = await store.load(world["own"])
    assert loaded.grant == _grant()
    assert loaded.failure is None
    assert loaded.version == 1
    assert loaded.owner_id == world["alice"]


async def test_grant_is_encrypted_at_rest_and_unreadable_under_another_key(world, db_factory):
    from sqlalchemy import select

    from claw.db.models import McpConnectorAuth

    await world["store"].put_grant(world["own"], world["alice"], _grant())
    async with db_factory() as db:
        raw = await db.scalar(select(McpConnectorAuth.grant_enc))
    assert raw.startswith("enc::")
    assert "at-1" not in raw and "rt-1" not in raw

    rotated = McpAuthStore(db_factory, SecretBox("other-secret"))
    loaded = await rotated.load(world["own"])
    assert loaded.grant is None
    assert loaded.failure is FailureReason.UNDECRYPTABLE


async def test_lease_is_a_compare_and_set_on_the_grant_version(world):
    store = world["store"]
    await store.put_grant(world["own"], world["alice"], _grant())

    assert await store.try_lease(world["own"], version=0, now=NOW) is None
    lease = await store.try_lease(world["own"], version=1, now=NOW)
    assert lease is not None
    assert await store.try_lease(world["own"], version=1, now=NOW + timedelta(seconds=5)) is None

    takeover = await store.try_lease(world["own"], version=1, now=NOW + LEASE_TTL + timedelta(seconds=1))
    assert takeover is not None
    assert takeover.owner != lease.owner

    assert await store.commit_under_lease(lease, _grant(access_token="stale-writer")) is False
    assert await store.commit_under_lease(takeover, _grant(access_token="at-2", refresh_token="rt-2")) is True
    loaded = await store.load(world["own"])
    assert loaded.grant.access_token == "at-2"
    assert loaded.grant.refresh_token == "rt-2"
    assert loaded.version == 2

    assert await store.mark_failed(world["own"], version=1, reason=FailureReason.REJECTED_TOKEN) is False
    assert (await store.load(world["own"])).failure is None
    assert await store.mark_failed(world["own"], version=2, reason=FailureReason.REJECTED_TOKEN) is True
    loaded = await store.load(world["own"])
    assert loaded.failure is FailureReason.REJECTED_TOKEN
    assert loaded.version == 3
    assert await store.try_lease(world["own"], version=3, now=NOW) is None


async def test_release_and_fail_under_lease(world):
    store = world["store"]
    await store.put_grant(world["own"], world["alice"], _grant())
    lease = await store.try_lease(world["own"], version=1, now=NOW)
    await store.release_lease(lease)
    again = await store.try_lease(world["own"], version=1, now=NOW)
    assert again is not None
    await store.fail_under_lease(again, FailureReason.INVALID_GRANT)
    loaded = await store.load(world["own"])
    assert loaded.failure is FailureReason.INVALID_GRANT
    assert loaded.grant == _grant()

    await store.put_grant(world["own"], world["alice"], _grant(access_token="at-3"))
    loaded = await store.load(world["own"])
    assert loaded.failure is None
    assert loaded.grant.access_token == "at-3"
    assert loaded.version == 3


async def test_admin_client_credentials_round_trip(world):
    store = world["store"]
    row_id = await store.upsert_admin_client(
        issuer="https://as.example.com",
        client_id="cid",
        client_secret="shh",
        auth_method="client_secret_basic",
        redirect_uri="https://claw.example.com/api/mcp-oauth/callback",
    )
    creds = await store.client_credentials(row_id)
    assert (creds.client_id, creds.client_secret, creds.auth_method) == ("cid", "shh", "client_secret_basic")
    same = await store.upsert_admin_client(
        issuer="https://as.example.com",
        client_id="cid2",
        client_secret="",
        auth_method="client_secret_post",
        redirect_uri="https://claw.example.com/api/mcp-oauth/callback",
    )
    assert same == row_id
    creds = await store.client_credentials(row_id)
    assert (creds.client_id, creds.client_secret, creds.auth_method) == ("cid2", "shh", "client_secret_post")
    assert await store.client_credentials("missing") is None
