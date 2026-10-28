"""Client identity fallbacks for MCP OAuth: an admin-registered client, else a
Client ID Metadata Document, else Dynamic Client Registration (RFC 7591).
Runs the same MockTransport authorization world as test_mcp_oauth_flow, with
real PinnedTransport SSRF checks against a fake resolver."""

import asyncio
import json
import time

import httpx
import pytest
from sqlalchemy import select

from claw.auth.mcp_oauth import McpOAuth, McpReauthorizationRequired
from claw.auth.mcp_oauth_discovery import (
    ClientStrategy,
    FailureReason,
    choose_client_strategy,
    is_publicly_reachable,
)
from claw.db.models import McpOAuthClient
from tests import test_mcp_oauth_flow as flow
from tests.test_mcp_oauth_flow import ADMIN, ALICE, BOB, ISSUER, PUBLIC, AuthWorld, _callback, _query, _start

# Shared fixtures, bound by name so pytest finds them in this module.
env = flow.env
admin_env = flow.admin_env
fake_dns = flow.fake_dns

DOC_URL = f"{PUBLIC}/api/mcp-oauth/client-metadata.json"
REDIRECT = f"{PUBLIC}/api/mcp-oauth/callback"
REGISTER = f"{ISSUER}/register"


class IdentityWorld(AuthWorld):
    """AuthWorld plus a registration endpoint that hands out a new client per
    call. `barrier`, when set, holds each registration until that many have
    arrived, so concurrent begins provably race."""

    def __init__(self) -> None:
        super().__init__()
        self.registrations: list[dict] = []
        self.secret_expires_at = 0
        self.barrier: int | None = None
        self.reply: httpx.Response | None = None
        self._arrived = asyncio.Event()

    def handler(self, request: httpx.Request):
        if request.url.path != "/register":
            return super().handler(request)
        self.requests.append((request.headers["Host"], "POST", "/register", {}, request.headers))
        self.registrations.append(json.loads(request.content))
        if self.reply is not None:
            return self.reply
        n = len(self.registrations)
        body = {
            **self.registrations[-1],
            "client_id": f"dcr-{n}",
            "client_secret": f"DCR-SECRET-{n}",
            "client_secret_expires_at": self.secret_expires_at,
        }
        if self.registrations[-1]["token_endpoint_auth_method"] == "none":
            del body["client_secret"]

        async def respond() -> httpx.Response:
            if self.barrier is not None:
                if n >= self.barrier:
                    self._arrived.set()
                await asyncio.wait_for(self._arrived.wait(), 5)
            return httpx.Response(201, json=body)

        return respond()


@pytest.fixture
async def ienv(env):
    """No admin client for ISSUER; the AS advertises neither CIMD nor DCR
    until a test turns them on."""
    world = IdentityWorld()
    env["world"] = world
    env["oauth"] = McpOAuth(env["store"], public_base_url=PUBLIC, http=world.http)
    env["app"].state.claw.mcp_oauth = env["oauth"]
    await env["store"].delete_admin_client(env["admin_client_id"])
    return env


def _enable_dcr(env, methods=("client_secret_post",), endpoint=REGISTER):
    env["world"].metadata["registration_endpoint"] = endpoint
    env["world"].metadata["token_endpoint_auth_methods_supported"] = list(methods)


async def _dcr_rows(env) -> list[McpOAuthClient]:
    async with env["store"].factory() as db:
        return list(await db.scalars(select(McpOAuthClient).where(McpOAuthClient.source == "dcr")))


async def _connect(env, headers=ALICE, connector_id=None) -> dict:
    started = await _start(env, headers=headers, connector_id=connector_id)
    assert started.status_code == 200, started.text
    params = _query(started.json()["authorize_url"])
    done = await _callback(env, state=params["state"], code="CODE-1")
    assert _query(done.headers["location"])["connector_status"] == "connected"
    return params


# ---------------------------------------------------------------- strategy table


@pytest.mark.parametrize(
    "admin, base, cimd, endpoint, methods, expected",
    [
        (True, PUBLIC, True, REGISTER, None, ClientStrategy.ADMIN),
        (True, "http://localhost:8700", False, None, None, ClientStrategy.ADMIN),
        (False, PUBLIC, True, REGISTER, None, ClientStrategy.METADATA_DOCUMENT),
        (False, PUBLIC, True, None, None, ClientStrategy.METADATA_DOCUMENT),
        (False, "http://claw.example.com", True, REGISTER, None, ClientStrategy.REGISTRATION),
        (False, "https://localhost:8700", True, REGISTER, None, ClientStrategy.REGISTRATION),
        (False, "https://127.0.0.1", True, REGISTER, None, ClientStrategy.REGISTRATION),
        (False, PUBLIC, False, REGISTER, None, ClientStrategy.REGISTRATION),
        (False, PUBLIC, False, REGISTER, ["none"], ClientStrategy.REGISTRATION),
        (False, PUBLIC, False, REGISTER, ["private_key_jwt"], None),
        (False, PUBLIC, False, None, None, None),
        (False, "https://localhost", True, None, None, None),
    ],
)
def test_client_strategy_table(admin, base, cimd, endpoint, methods, expected):
    assert (
        choose_client_strategy(
            has_admin_client=admin,
            public_base_url=base,
            cimd_supported=cimd,
            registration_endpoint=endpoint,
            token_auth_methods=methods,
        )
        is expected
    )


@pytest.mark.parametrize(
    "base, reachable",
    [
        ("https://claw.example.com", True),
        ("https://claw.example.com:8443/", True),
        ("https://93.184.216.34", True),
        ("http://claw.example.com", False),
        ("https://localhost", False),
        ("https://LOCALHOST.", False),
        ("https://app.localhost", False),
        ("https://claw.local", False),
        ("https://claw.corp.internal", False),
        ("https://claw", False),
        ("https://10.0.0.7", False),
        ("https://192.168.1.4", False),
        ("https://[::1]", False),
        ("https://169.254.169.254", False),
    ],
)
def test_cimd_needs_a_base_url_the_authorization_server_can_reach(base, reachable):
    assert is_publicly_reachable(base) is reachable


# ---------------------------------------------------------------- CIMD


async def test_client_metadata_document_is_public_and_names_itself(ienv):
    doc = await ienv["c"].get("/api/mcp-oauth/client-metadata.json")
    assert doc.status_code == 200
    assert doc.json() == {
        "client_id": DOC_URL,
        "client_name": "Softnix PrivateClaw",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


async def test_cimd_flow_uses_the_document_url_as_a_public_client(ienv):
    ienv["world"].metadata["client_id_metadata_document_supported"] = True
    _enable_dcr(ienv)
    params = await _connect(ienv)

    assert params["client_id"] == DOC_URL
    ((_, _, _, form, headers),) = ienv["world"].hits("/token")
    assert form["client_id"] == DOC_URL
    assert "client_secret" not in form and "Authorization" not in headers
    assert ienv["world"].registrations == []
    assert await _dcr_rows(ienv) == []

    ienv["world"].token = {**ienv["world"].token, "access_token": "AT-2"}
    assert await ienv["oauth"].access_token(ienv["connector"], stale="AT-SECRET") == "AT-2"
    _, (_, _, _, refresh, refresh_headers) = ienv["world"].hits("/token")
    assert refresh["client_id"] == DOC_URL
    assert "client_secret" not in refresh and "Authorization" not in refresh_headers


async def test_a_localhost_base_url_falls_back_from_cimd_to_registration(ienv):
    oauth = McpOAuth(ienv["store"], public_base_url="https://localhost:8700", http=ienv["world"].http)
    ienv["app"].state.claw.mcp_oauth = oauth
    ienv["world"].metadata["client_id_metadata_document_supported"] = True
    _enable_dcr(ienv)
    started = await _start(ienv)
    assert started.status_code == 200, started.text
    assert _query(started.json()["authorize_url"])["client_id"] == "dcr-1"


# ---------------------------------------------------------------- DCR


async def test_dcr_flow_registers_once_and_authenticates_with_the_issued_secret(ienv):
    _enable_dcr(ienv, methods=["client_secret_post", "private_key_jwt"])
    params = await _connect(ienv)

    assert params["client_id"] == "dcr-1"
    assert ienv["world"].registrations == [
        {
            "client_name": "Softnix PrivateClaw",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:tools",
        }
    ]
    (row,) = await _dcr_rows(ienv)
    assert row.issuer == ISSUER and row.redirect_uri == REDIRECT and row.client_id == "dcr-1"
    assert row.client_secret_enc and "DCR-SECRET" not in row.client_secret_enc
    assert "DCR-SECRET" not in (row.registration_enc or "")

    ((_, _, _, form, headers),) = ienv["world"].hits("/token")
    assert form["client_id"] == "dcr-1" and form["client_secret"] == "DCR-SECRET-1"
    assert "Authorization" not in headers


async def test_dcr_prefers_a_public_client_when_the_server_allows_one(ienv):
    _enable_dcr(ienv, methods=["client_secret_basic", "none"])
    await _connect(ienv)
    assert ienv["world"].registrations[0]["token_endpoint_auth_method"] == "none"
    (row,) = await _dcr_rows(ienv)
    assert row.client_secret_enc is None and row.token_endpoint_auth_method == "none"
    ((_, _, _, form, headers),) = ienv["world"].hits("/token")
    assert form["client_id"] == "dcr-1"
    assert "client_secret" not in form and "Authorization" not in headers


async def test_two_users_on_one_issuer_share_one_registration(ienv):
    _enable_dcr(ienv)
    bob_user = await ienv["app"].state.claw.users.get_by_email("bob@x.io")
    bob_connector = await ienv["connectors"].upsert(
        bob_user.id, "remote-bob", kind="mcp", transport="http", url=ienv["connector"].url
    )
    alice = await _connect(ienv)
    bob_params = await _connect(ienv, headers=BOB, connector_id=bob_connector.id)

    assert alice["client_id"] == bob_params["client_id"] == "dcr-1"
    assert len(ienv["world"].registrations) == 1
    assert len(await _dcr_rows(ienv)) == 1


async def test_concurrent_registrations_converge_on_one_row(ienv):
    _enable_dcr(ienv)
    ienv["world"].barrier = 2
    second = await ienv["connectors"].upsert(
        ienv["alice"].id, "remote-2", kind="mcp", transport="http", url=ienv["connector"].url
    )
    a, b = await asyncio.gather(_start(ienv), _start(ienv, connector_id=second.id))

    assert a.status_code == b.status_code == 200
    assert len(ienv["world"].registrations) == 2  # both raced to the AS
    (row,) = await _dcr_rows(ienv)
    ids = {_query(r.json()["authorize_url"])["client_id"] for r in (a, b)}
    assert ids == {row.client_id}


async def test_an_expired_registration_is_replaced(ienv):
    _enable_dcr(ienv)
    ienv["world"].secret_expires_at = int(time.time()) - 60
    first = await _connect(ienv)
    ienv["world"].secret_expires_at = 0
    second = await _connect(ienv)

    assert (first["client_id"], second["client_id"]) == ("dcr-1", "dcr-2")
    rows = await _dcr_rows(ienv)
    assert {(row.client_id, row.retired_at is None) for row in rows} == {("dcr-1", False), ("dcr-2", True)}
    third = await _connect(ienv)
    assert third["client_id"] == "dcr-2" and len(ienv["world"].registrations) == 2


async def test_invalid_client_on_refresh_retires_the_registration(ienv):
    _enable_dcr(ienv)
    await _connect(ienv)
    ienv["world"].routes[("as.example.com", "POST", "/token")] = lambda: httpx.Response(
        400, json={"error": "invalid_client"}
    )
    with pytest.raises(McpReauthorizationRequired):
        await ienv["oauth"].access_token(ienv["connector"], stale="AT-SECRET")

    (retired,) = await _dcr_rows(ienv)
    assert retired.retired_at is not None
    loaded = await ienv["store"].load(ienv["connector"].id)
    assert loaded.failure is FailureReason.INVALID_CLIENT
    (row,) = (await ienv["c"].get("/api/connectors", headers=ALICE)).json()
    assert row["oauth_grant"]["state"] == "reauthorization_required"
    assert len(ienv["world"].registrations) == 1  # refresh never registers

    ienv["world"].routes[("as.example.com", "POST", "/token")] = lambda: httpx.Response(
        200, json=ienv["world"].token
    )
    assert (await _connect(ienv))["client_id"] == "dcr-2"


class ScriptedToken:
    """A token endpoint that answers each call with the next scripted
    response, the last one repeating, and records which client called."""

    def __init__(self, world: AuthWorld, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.world = world
        world.routes[("as.example.com", "POST", "/token")] = self

    def __call__(self) -> httpx.Response:
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def client_ids(self) -> list[str]:
        return [form["client_id"] for _, _, _, form, _ in self.world.hits("/token")]


async def _bob_connector(env):
    bob = await env["app"].state.claw.users.get_by_email("bob@x.io")
    return await env["connectors"].upsert(
        bob.id, "remote-bob", kind="mcp", transport="http", url=env["connector"].url
    )


async def test_invalid_client_for_one_user_leaves_the_shared_registration_to_the_others(ienv):
    _enable_dcr(ienv)
    bob_connector = await _bob_connector(ienv)
    await _connect(ienv)
    await _connect(ienv, headers=BOB, connector_id=bob_connector.id)
    token = ScriptedToken(
        ienv["world"],
        httpx.Response(400, json={"error": "invalid_client"}),
        httpx.Response(200, json={**ienv["world"].token, "access_token": "AT-BOB-2"}),
    )

    with pytest.raises(McpReauthorizationRequired):
        await ienv["oauth"].access_token(ienv["connector"], stale="AT-SECRET")
    assert await ienv["oauth"].access_token(bob_connector, stale="AT-SECRET") == "AT-BOB-2"

    assert (await ienv["store"].load(ienv["connector"].id)).failure is FailureReason.INVALID_CLIENT
    assert (await ienv["store"].load(bob_connector.id)).failure is None
    assert token.client_ids()[-2:] == ["dcr-1", "dcr-1"]
    # Alice's Reconnect registers afresh instead of reusing the refused client.
    assert (await _connect(ienv))["client_id"] == "dcr-2"
    assert len(await _dcr_rows(ienv)) == 2


async def test_invalid_client_on_one_code_exchange_leaves_other_grants_working(ienv):
    _enable_dcr(ienv)
    bob_connector = await _bob_connector(ienv)
    await _connect(ienv)
    ScriptedToken(
        ienv["world"],
        httpx.Response(401, json={"error": "invalid_client"}),
        httpx.Response(200, json={**ienv["world"].token, "access_token": "AT-ALICE-2"}),
    )
    started = await _start(ienv, headers=BOB, connector_id=bob_connector.id)
    done = await _callback(ienv, state=_query(started.json()["authorize_url"])["state"], code="CODE-B")
    assert _query(done.headers["location"])["connector_status"] == "token_failed"

    assert await ienv["oauth"].access_token(ienv["connector"], stale="AT-SECRET") == "AT-ALICE-2"
    assert (await ienv["store"].load(ienv["connector"].id)).failure is None


async def _corrupt_secret(env, row_id: str) -> None:
    async with env["store"].factory() as db:
        row = await db.get(McpOAuthClient, row_id)
        row.client_secret_enc = "enc::not-a-fernet-token"
        await db.commit()


async def test_an_unreadable_registration_secret_is_replaced_on_connect(ienv):
    _enable_dcr(ienv)
    await _connect(ienv)
    (row,) = await _dcr_rows(ienv)
    await _corrupt_secret(ienv, row.id)

    assert (await _connect(ienv))["client_id"] == "dcr-2"
    ((_, _, _, form, _),) = ienv["world"].hits("/token")[-1:]
    assert form["client_secret"] == "DCR-SECRET-2"


async def test_an_unreadable_client_secret_never_goes_on_the_wire(env):
    await _connect(env)
    await _corrupt_secret(env, env["admin_client_id"])
    token_calls = len(env["world"].hits("/token"))

    with pytest.raises(McpReauthorizationRequired) as raised:
        await env["app"].state.claw.mcp_oauth.access_token(env["connector"], stale="AT-SECRET")

    assert raised.value.reason is FailureReason.UNDECRYPTABLE
    assert len(env["world"].hits("/token")) == token_calls


async def test_an_unreadable_admin_client_is_a_clear_start_error(env):
    await _corrupt_secret(env, env["admin_client_id"])

    started = await _start(env)

    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "client_unreadable"
    assert "administrator" in started.json()["detail"]["message"]


async def test_an_unreadable_admin_secret_does_not_count_as_stored(admin_env):
    await _corrupt_secret(admin_env, admin_env["admin_client_id"])
    body = {"issuer": ISSUER, "client_id": "claw-client", "token_endpoint_auth_method": "client_secret_basic"}

    put = await admin_env["c"].put("/api/admin/mcp-oauth-clients", json=body, headers=ADMIN)
    listed = await admin_env["c"].get("/api/admin/mcp-oauth-clients", headers=ADMIN)

    assert put.status_code == 422
    (row,) = listed.json()["clients"]
    assert row["has_client_secret"] is False


async def test_invalid_client_never_deletes_an_admin_client(env):
    params = _query((await _start(env)).json()["authorize_url"])
    await _callback(env, state=params["state"], code="CODE-1")
    env["world"].routes[("as.example.com", "POST", "/token")] = lambda: httpx.Response(
        401, json={"error": "invalid_client"}
    )
    with pytest.raises(McpReauthorizationRequired):
        await env["app"].state.claw.mcp_oauth.access_token(env["connector"], stale="AT-SECRET")
    assert await env["store"].client_credentials(env["admin_client_id"]) is not None


async def test_refresh_and_bearer_paths_never_register(ienv):
    _enable_dcr(ienv)
    assert await ienv["oauth"].access_token(ienv["connector"]) is None
    await _connect(ienv)
    ienv["world"].token = {**ienv["world"].token, "access_token": "AT-2"}
    assert await ienv["oauth"].access_token(ienv["connector"], stale="AT-SECRET") == "AT-2"

    request = httpx.Request("POST", ienv["connector"].url)
    flow = ienv["oauth"].http_auth(ienv["connector"]).async_auth_flow(request)
    sent = await flow.__anext__()
    assert sent.headers["Authorization"] == "Bearer AT-2"
    assert len(ienv["world"].registrations) == 1


async def test_admin_client_beats_cimd_and_dcr(env):
    env["world"].metadata["client_id_metadata_document_supported"] = True
    env["world"].metadata["registration_endpoint"] = REGISTER
    started = await _start(env)
    assert _query(started.json()["authorize_url"])["client_id"] == "claw-client"
    assert env["world"].hits("/register") == []


@pytest.mark.parametrize(
    "endpoint, message",
    [
        ("https://as-internal.example.com/register", "not a public HTTPS address"),
        ("http://as.example.com/register", "not HTTPS"),
    ],
)
async def test_an_unsafe_registration_endpoint_is_never_dialled(ienv, endpoint, message):
    _enable_dcr(ienv, endpoint=endpoint)
    started = await _start(ienv)
    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "registration_failed"
    assert message in started.json()["detail"]["message"]
    assert ienv["world"].registrations == []
    assert await _dcr_rows(ienv) == []


async def test_a_malformed_registration_response_stores_nothing(ienv):
    _enable_dcr(ienv)
    ienv["world"].reply = httpx.Response(201, json={"redirect_uris": [REDIRECT]})
    started = await _start(ienv)
    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "registration_failed"
    assert await _dcr_rows(ienv) == []
    assert (await ienv["store"].load(ienv["connector"].id)) is None


async def test_no_identity_option_asks_for_an_admin(ienv):
    started = await _start(ienv)
    assert started.status_code == 400
    detail = started.json()["detail"]
    assert detail["code"] == "no_client"
    assert ISSUER in detail["message"] and "administrator" in detail["message"]


async def test_admin_client_list_hides_registrations(admin_env, ienv):
    _enable_dcr(ienv)
    await _connect(ienv)
    listed = await ienv["c"].get("/api/admin/mcp-oauth-clients", headers=ADMIN)
    assert listed.status_code == 200
    assert listed.json()["clients"] == []
    assert "DCR-SECRET" not in listed.text
