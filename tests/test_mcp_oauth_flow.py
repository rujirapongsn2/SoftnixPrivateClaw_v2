"""OAuth for per-user remote MCP connectors, end to end through FastAPI: start
(discovery, PKCE, state), the public callback, disconnect/revoke, the
connector listing, and the admin client API. Every outbound dial goes through
the real PinnedTransport into a MockTransport authorization world, with DNS
answered by a fake resolver so SSRF checks run for real."""

import asyncio
import base64
import hashlib
import socket
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from claw.auth.mcp_oauth import McpOAuth
from claw.auth.tokens import create_access_token
from claw.config import Settings
from claw.db.models import Base
from claw.main import create_app
from claw.auth.mcp_oauth_discovery import FailureReason, choose_client_strategy, ClientStrategy
from claw.db.stores import ConnectorStore, McpAuthStore, UserStore
from claw.security.crypto import SecretBox
from claw.security.ssrf import pinned_client
from tests.conftest_app import build_api_app

PUBLIC = "https://claw.example.com"
SERVER = "https://mcp.example.com/mcp"
ISSUER = "https://as.example.com"
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
NONCE_COOKIE = "claw_mcp_oauth_nonce"
DNS = {
    "mcp.example.com": "93.184.216.34",
    "as.example.com": "93.184.216.35",
    "as-internal.example.com": "10.0.0.7",
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    async def getaddrinfo(self, host, port, *args, **kwargs):
        if host not in DNS:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (DNS[host], 0))]

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", getaddrinfo)


class AuthWorld:
    """An MCP server and its authorization server. `routes` maps (host,
    method, path) to a response or a callable; tests override entries."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.requests: list[tuple[str, str, str, dict, httpx.Headers]] = []
        as_host = urlsplit(issuer).hostname
        self.metadata = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "revocation_endpoint": f"{issuer}/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }
        self.prm = {"resource": SERVER, "authorization_servers": [issuer], "scopes_supported": ["mcp:read"]}
        self.token = {
            "access_token": "AT-SECRET",
            "refresh_token": "RT-SECRET",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        self.routes = {
            ("mcp.example.com", "POST", "/mcp"): lambda: httpx.Response(
                401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{PRM_URL}", scope="mcp:tools"'},
            ),
            ("mcp.example.com", "GET", "/.well-known/oauth-protected-resource/mcp"): lambda: httpx.Response(
                200, json=self.prm
            ),
            (as_host, "GET", "/.well-known/oauth-authorization-server"): lambda: httpx.Response(
                200, json=self.metadata
            ),
            (as_host, "POST", "/token"): lambda: httpx.Response(200, json=self.token),
            (as_host, "POST", "/revoke"): lambda: httpx.Response(200),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["Host"].split(":")[0]
        form = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" else {}
        self.requests.append((host, request.method, request.url.path, form, request.headers))
        route = self.routes.get((host, request.method, request.url.path))
        return route() if route is not None else httpx.Response(404)

    def http(self) -> httpx.AsyncClient:
        return pinned_client(require_https=True, inner=lambda: httpx.MockTransport(self.handler))

    def hits(self, path: str) -> list[tuple[str, str, str, dict, httpx.Headers]]:
        return [r for r in self.requests if r[2] == path]


def _headers(email: str) -> dict:
    return {"Authorization": "Bearer t", "X-User-Email": email}


ALICE = _headers("alice@x.io")
BOB = _headers("bob@x.io")


@pytest.fixture
async def env(db_factory):
    app = build_api_app(db_factory, public_base_url=PUBLIC, web_base_url=PUBLIC)
    world = AuthWorld()
    store = McpAuthStore(db_factory, SecretBox("test-secret"))
    app.state.claw.mcp_oauth = McpOAuth(store, public_base_url=PUBLIC, http=world.http)
    users = UserStore(db_factory)
    alice = await users.get_or_create_by_email("alice@x.io")
    await users.get_or_create_by_email("bob@x.io")
    connectors = ConnectorStore(db_factory)
    connector = await connectors.upsert(alice.id, "remote", kind="mcp", transport="http", url=SERVER)
    admin_client_id = await store.upsert_admin_client(
        issuer=ISSUER,
        client_id="claw-client",
        client_secret="CLIENT-SECRET",
        auth_method="client_secret_basic",
        redirect_uri=f"{PUBLIC}/api/mcp-oauth/callback",
    )
    # https, so the Secure nonce cookie round-trips the way a browser sends it.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as c:
        yield {
            "app": app,
            "c": c,
            "world": world,
            "store": store,
            "connectors": connectors,
            "alice": alice,
            "connector": connector,
            "admin_client_id": admin_client_id,
        }


async def _start(env, headers=ALICE, connector_id=None) -> httpx.Response:
    return await env["c"].post(
        f"/api/connectors/{connector_id or env['connector'].id}/authorization", headers=headers
    )


async def _callback(env, **params) -> httpx.Response:
    return await env["c"].get("/api/mcp-oauth/callback", params=params)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _authorized(env) -> tuple[dict, httpx.Response]:
    started = await _start(env)
    assert started.status_code == 200, started.text
    params = _query(started.json()["authorize_url"])
    done = await _callback(env, state=params["state"], code="CODE-1")
    return params, done


async def test_full_flow_connects_and_the_listing_shows_it(env):
    started = await _start(env)
    assert started.status_code == 200
    authorize_url = started.json()["authorize_url"]
    assert authorize_url.startswith(f"{ISSUER}/authorize?")
    params = _query(authorize_url)
    assert params["response_type"] == "code"
    assert params["client_id"] == "claw-client"
    assert params["redirect_uri"] == f"{PUBLIC}/api/mcp-oauth/callback"
    assert params["code_challenge_method"] == "S256"
    assert len(params["code_challenge"]) == 43
    assert params["resource"] == SERVER
    assert params["scope"] == "mcp:tools"  # the challenge's scope wins over PRM's
    assert len(params["state"]) >= 43
    cookie = started.headers["set-cookie"]
    assert f"{NONCE_COOKIE}=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "Path=/api/mcp-oauth/callback" in cookie and "SameSite=lax" in cookie

    done = await _callback(env, state=params["state"], code="CODE-1")

    assert done.status_code == 303
    assert done.headers["location"] == (
        f"{PUBLIC}/?connector_id={env['connector'].id}&connector_status=connected"
    )
    assert f'{NONCE_COOKIE}=""' in done.headers["set-cookie"]
    ((_, _, _, form, headers),) = env["world"].hits("/token")
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "CODE-1"
    assert form["redirect_uri"] == f"{PUBLIC}/api/mcp-oauth/callback"
    assert form["resource"] == SERVER
    verifier = form["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == params["code_challenge"]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"claw-client:CLIENT-SECRET").decode()
    assert "client_secret" not in form

    loaded = await env["store"].load(env["connector"].id)
    assert loaded.grant.access_token == "AT-SECRET"
    assert loaded.grant.refresh_token == "RT-SECRET"
    assert loaded.grant.scope == "mcp:tools"

    listing = await env["c"].get("/api/connectors", headers=ALICE)
    (row,) = listing.json()
    assert row["oauth_grant"] == {
        "state": "connected",
        "issuer": ISSUER,
        "scope": "mcp:tools",
        "can_revoke": True,
        "failure": None,
    }
    for body in (started.text, done.headers["location"], listing.text):
        for secret in ("AT-SECRET", "RT-SECRET", verifier, "CLIENT-SECRET", "CODE-1"):
            assert secret not in body


async def test_prm_scopes_are_used_when_the_challenge_has_none(env):
    env["world"].routes[("mcp.example.com", "POST", "/mcp")] = lambda: httpx.Response(
        401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{PRM_URL}"'}
    )
    started = await _start(env)
    assert _query(started.json()["authorize_url"])["scope"] == "mcp:read"


async def test_well_known_prm_is_found_without_a_resource_metadata_hint(env):
    env["world"].routes[("mcp.example.com", "POST", "/mcp")] = lambda: httpx.Response(
        401, headers={"WWW-Authenticate": "Bearer"}
    )
    started = await _start(env)
    assert started.status_code == 200
    assert env["world"].hits("/.well-known/oauth-protected-resource/mcp")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda w: w.metadata.update(issuer="https://evil.example.com"), "different issuer"),
        (lambda w: w.prm.update(resource="https://mcp.example.com/other"), "different resource"),
        (lambda w: w.metadata.pop("code_challenge_methods_supported"), "S256"),
        (lambda w: w.metadata.update(code_challenge_methods_supported=["plain"]), "S256"),
        (lambda w: w.metadata.update(token_endpoint="http://as.example.com/token"), "not served over HTTPS"),
        (lambda w: w.metadata.update(authorization_endpoint="http://as.example.com/authorize"), "HTTPS"),
    ],
)
async def test_discovery_rejects_untrustworthy_metadata(env, mutate, message):
    mutate(env["world"])
    started = await _start(env)
    assert started.status_code == 400
    detail = started.json()["detail"]
    assert detail["code"] == "discovery_failed"
    assert message in detail["message"]
    assert "set-cookie" not in started.headers
    assert (await env["store"].load(env["connector"].id)) is None


async def test_prm_for_a_parent_path_is_accepted(env):
    env["world"].prm["resource"] = "https://mcp.example.com/"
    assert (await _start(env)).status_code == 200


async def test_a_redirect_during_discovery_is_refused_not_followed(env):
    env["world"].routes[("mcp.example.com", "GET", "/.well-known/oauth-protected-resource/mcp")] = lambda: (
        httpx.Response(302, headers={"Location": "https://as.example.com/prm"})
    )
    started = await _start(env)
    assert started.status_code == 400
    assert "redirected" in started.json()["detail"]["message"]
    assert not env["world"].hits("/prm")


async def test_a_private_ip_authorization_server_is_never_dialled(env):
    internal = AuthWorld(issuer="https://as-internal.example.com")
    env["world"].prm["authorization_servers"] = ["https://as-internal.example.com"]
    env["world"].routes.update(internal.routes)
    started = await _start(env)
    assert started.status_code == 400
    assert "not a public HTTPS address" in started.json()["detail"]["message"]
    assert not [r for r in env["world"].requests if r[0] == "as-internal.example.com"]


async def test_a_server_that_does_not_challenge_cannot_start(env):
    env["world"].routes[("mcp.example.com", "POST", "/mcp")] = lambda: httpx.Response(200, json={})
    started = await _start(env)
    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "discovery_failed"


async def test_no_admin_client_for_the_issuer_is_a_clear_error(env):
    await env["store"].delete_admin_client(env["admin_client_id"])
    started = await _start(env)
    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "no_client"
    assert ISSUER in started.json()["detail"]["message"]


async def test_plain_http_server_is_refused(env):
    connector = await env["connectors"].upsert(
        env["alice"].id, "plain", kind="mcp", transport="http", url="http://mcp.example.com/mcp"
    )
    started = await _start(env, connector_id=connector.id)
    assert started.status_code == 400
    assert started.json()["detail"]["code"] == "https_required"
    assert env["world"].requests == []


async def test_admin_global_connector_is_404(env):
    connector = await env["connectors"].upsert(None, "shared", kind="mcp", transport="http", url=SERVER)
    assert (await _start(env, connector_id=connector.id)).status_code == 404
    assert (
        await env["c"].delete(f"/api/connectors/{connector.id}/authorization", headers=ALICE)
    ).status_code == 404
    assert env["world"].requests == []


async def test_another_users_connector_is_404(env):
    assert (await _start(env, headers=BOB)).status_code == 404
    assert (
        await env["c"].delete(f"/api/connectors/{env['connector'].id}/authorization", headers=BOB)
    ).status_code == 404
    assert env["world"].requests == []


async def test_state_is_single_use(env):
    params, done = await _authorized(env)
    assert _query(done.headers["location"])["connector_status"] == "connected"
    replay = await _callback(env, state=params["state"], code="CODE-1")
    assert _query(replay.headers["location"]) == {"connector_status": "expired"}
    assert len(env["world"].hits("/token")) == 1


async def test_expired_state_is_refused(env):
    oauth = env["app"].state.claw.mcp_oauth
    started = await _start(env)
    real_now = oauth._now
    oauth._now = lambda: real_now() + timedelta(minutes=11)
    done = await _callback(env, state=_query(started.json()["authorize_url"])["state"], code="CODE-1")
    assert _query(done.headers["location"]) == {"connector_status": "expired"}
    assert env["world"].hits("/token") == []


async def test_an_unknown_state_is_expired(env):
    done = await _callback(env, state="forged", code="CODE-1")
    assert done.status_code == 303
    assert _query(done.headers["location"]) == {"connector_status": "expired"}


async def test_a_callback_from_another_browser_is_refused_and_burns_the_flow(env):
    started = await _start(env)
    state = _query(started.json()["authorize_url"])["state"]
    env["c"].cookies.clear()
    env["c"].cookies.set(NONCE_COOKIE, "attacker-nonce", path="/api/mcp-oauth/callback")
    done = await _callback(env, state=state, code="CODE-1")
    assert _query(done.headers["location"])["connector_status"] == "browser_mismatch"
    assert env["world"].hits("/token") == []
    assert (await env["store"].load(env["connector"].id)).grant is None


async def test_access_denied_is_reported_and_stores_nothing(env):
    started = await _start(env)
    state = _query(started.json()["authorize_url"])["state"]
    done = await _callback(env, state=state, error="access_denied")
    assert _query(done.headers["location"]) == {
        "connector_id": env["connector"].id,
        "connector_status": "denied",
    }
    assert env["world"].hits("/token") == []


async def test_token_endpoint_error_is_token_failed(env):
    env["world"].routes[("as.example.com", "POST", "/token")] = lambda: httpx.Response(
        400, json={"error": "invalid_grant"}
    )
    _, done = await _authorized(env)
    assert _query(done.headers["location"])["connector_status"] == "token_failed"
    assert (await env["store"].load(env["connector"].id)).grant is None


async def test_the_last_start_wins(env):
    first = _query((await _start(env)).json()["authorize_url"])["state"]
    second = await _start(env)
    stale = await _callback(env, state=first, code="CODE-1")
    assert _query(stale.headers["location"])["connector_status"] == "expired"
    done = await _callback(env, state=_query(second.json()["authorize_url"])["state"], code="CODE-2")
    assert _query(done.headers["location"])["connector_status"] == "connected"


async def test_callback_never_500s(env, monkeypatch):
    async def boom(**_):
        raise RuntimeError("database is down")

    monkeypatch.setattr(env["app"].state.claw.mcp_oauth, "complete", boom)
    done = await _callback(env, state="x", code="y")
    assert done.status_code == 303
    assert _query(done.headers["location"]) == {"connector_status": "error"}


async def test_revoke_calls_the_revocation_endpoint_and_forgets_locally(env):
    await _authorized(env)
    gone = await env["c"].delete(
        f"/api/connectors/{env['connector'].id}/authorization", params={"revoke": "true"}, headers=ALICE
    )
    assert gone.json() == {"disconnected": True, "revoked": True}
    revocations = env["world"].hits("/revoke")
    assert [(r[3]["token"], r[3]["token_type_hint"]) for r in revocations] == [
        ("RT-SECRET", "refresh_token"),
        ("AT-SECRET", "access_token"),
    ]
    assert all(r[4]["Authorization"].startswith("Basic ") for r in revocations)
    assert (await env["store"].load(env["connector"].id)) is None
    assert "AT-SECRET" not in gone.text


async def test_a_failing_revocation_still_forgets_locally(env):
    await _authorized(env)
    env["world"].routes[("as.example.com", "POST", "/revoke")] = lambda: httpx.Response(503)
    gone = await env["c"].delete(
        f"/api/connectors/{env['connector'].id}/authorization", params={"revoke": "true"}, headers=ALICE
    )
    assert gone.status_code == 200
    assert gone.json() == {"disconnected": True, "revoked": False}
    assert (await env["store"].load(env["connector"].id)) is None


async def test_disconnect_without_revoke_never_calls_the_server(env):
    await _authorized(env)
    gone = await env["c"].delete(f"/api/connectors/{env['connector'].id}/authorization", headers=ALICE)
    assert gone.json() == {"disconnected": True, "revoked": False}
    assert env["world"].hits("/revoke") == []
    assert (await env["store"].load(env["connector"].id)) is None


async def test_persisted_failure_beats_a_stale_connected_runtime_status(env):
    await _authorized(env)
    state = env["app"].state.claw
    loaded = await env["store"].load(env["connector"].id)
    await env["store"].mark_failed(env["connector"].id, loaded.version, FailureReason.REJECTED_TOKEN)

    async def stale_connected(user_id):
        return {"remote": {"status": "connected"}}

    state.connectors_mgr.status = stale_connected
    (row,) = (await env["c"].get("/api/connectors", headers=ALICE)).json()
    assert row["runtime"]["status"] == "reauthorization_required"
    assert row["oauth_grant"]["state"] == "reauthorization_required"
    assert row["oauth_grant"]["failure"] == "rejected_token"


async def test_listing_without_any_flow_has_no_grant(env):
    (row,) = (await env["c"].get("/api/connectors", headers=ALICE)).json()
    assert row["oauth_grant"] is None


def test_client_strategy_is_admin_or_nothing():
    assert choose_client_strategy(has_admin_client=True) is ClientStrategy.ADMIN
    assert choose_client_strategy(has_admin_client=False) is None


# ---------------------------------------------------------------- admin client API


ADMIN = _headers("root@x.io")


@pytest.fixture
async def admin_env(env, db_factory):
    users = UserStore(db_factory)
    await users.create("root@x.io", is_admin=True)
    return env


async def test_admin_client_secret_is_write_only_and_kept_on_empty_put(admin_env):
    c = admin_env["c"]
    put = await c.put(
        "/api/admin/mcp-oauth-clients",
        json={
            "issuer": "https://other-as.example.com/",
            "client_id": "cid",
            "client_secret": "TOP-SECRET",
            "token_endpoint_auth_method": "client_secret_post",
        },
        headers=ADMIN,
    )
    assert put.status_code == 200
    assert "TOP-SECRET" not in put.text
    body = put.json()
    assert body["redirect_uri"] == f"{PUBLIC}/api/mcp-oauth/callback"
    row = next(r for r in body["clients"] if r["issuer"] == "https://other-as.example.com")
    assert row == {
        "id": row["id"],
        "issuer": "https://other-as.example.com",
        "client_id": "cid",
        "token_endpoint_auth_method": "client_secret_post",
        "has_client_secret": True,
        "redirect_uri": f"{PUBLIC}/api/mcp-oauth/callback",
    }

    again = await c.put(
        "/api/admin/mcp-oauth-clients",
        json={"issuer": "https://other-as.example.com", "client_id": "cid2", "client_secret": ""},
        headers=ADMIN,
    )
    assert again.status_code == 200
    credentials = await admin_env["store"].client_credentials(row["id"])
    assert credentials.client_id == "cid2" and credentials.client_secret == "TOP-SECRET"

    listed = await c.get("/api/admin/mcp-oauth-clients", headers=ADMIN)
    assert "TOP-SECRET" not in listed.text and "CLIENT-SECRET" not in listed.text

    assert (await c.delete(f"/api/admin/mcp-oauth-clients/{row['id']}", headers=ADMIN)).status_code == 200
    assert (await c.delete(f"/api/admin/mcp-oauth-clients/{row['id']}", headers=ADMIN)).status_code == 404


async def test_admin_client_validation(admin_env):
    c = admin_env["c"]
    insecure = await c.put(
        "/api/admin/mcp-oauth-clients",
        json={"issuer": "http://as.example.com", "client_id": "x", "client_secret": "s"},
        headers=ADMIN,
    )
    assert insecure.status_code == 422
    no_secret = await c.put(
        "/api/admin/mcp-oauth-clients",
        json={"issuer": "https://new-as.example.com", "client_id": "x"},
        headers=ADMIN,
    )
    assert no_secret.status_code == 422
    public = await c.put(
        "/api/admin/mcp-oauth-clients",
        json={"issuer": "https://new-as.example.com", "client_id": "x", "token_endpoint_auth_method": "none"},
        headers=ADMIN,
    )
    assert public.status_code == 200


async def test_admin_client_api_is_admin_only(admin_env):
    c = admin_env["c"]
    assert (await c.get("/api/admin/mcp-oauth-clients", headers=ALICE)).status_code == 403
    assert (
        await c.put(
            "/api/admin/mcp-oauth-clients",
            json={"issuer": ISSUER, "client_id": "evil", "client_secret": "s"},
            headers=ALICE,
        )
    ).status_code == 403
    assert (
        await c.delete(f"/api/admin/mcp-oauth-clients/{admin_env['admin_client_id']}", headers=ALICE)
    ).status_code == 403


# ---------------------------------------------------------------- real app wiring


@pytest.fixture
async def integrated(tmp_path):
    """create_app itself, with Bot Mode mounted, so the routes tested are the
    ones the running service registers."""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/mode.db",
        secret_key="test-mode-secret" * 3,
        auto_migrate=False,
        public_base_url=PUBLIC,
        web_base_url=PUBLIC,
        sbot_enabled=True,
        workspaces_root=tmp_path / "workspaces",
        knowledge_root=tmp_path / "knowledge",
        branding_root=tmp_path / "branding",
        blueprints_root=tmp_path / "blueprints",
        sandbox={"enabled": False},
        llm={"api_key": "test-key"},
    )
    app = create_app(settings)
    host, mode = app.state.claw, app.state.sbot
    engine = host.users.factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    world = AuthWorld()
    host.mcp_oauth._http = world.http
    user = await host.users.get_or_create_by_email("wired@example.test")
    connector = await host.connectors.upsert(user.id, "remote", kind="mcp", transport="http", url=SERVER)
    await host.mcp_oauth.store.upsert_admin_client(
        issuer=ISSUER,
        client_id="claw-client",
        client_secret="CLIENT-SECRET",
        auth_method="client_secret_basic",
        redirect_uri=f"{PUBLIC}/api/mcp-oauth/callback",
    )
    token = create_access_token(user.id, settings.secret_key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        yield {"c": c, "connector": connector, "host": host, "mode": mode}
    await host.runtime.drain()
    await mode.runtime.drain()
    await engine.dispose()


@pytest.mark.parametrize("prefix, web_root", [("/api", "/"), ("/modes/sbot/api", "/chat/sbot")])
async def test_both_mounts_start_a_flow_that_returns_to_their_own_app(integrated, prefix, web_root):
    assert integrated["host"].mcp_oauth is integrated["mode"].mcp_oauth
    connector_id = integrated["connector"].id
    started = await integrated["c"].post(f"{prefix}/connectors/{connector_id}/authorization")
    assert started.status_code == 200, started.text
    state = _query(started.json()["authorize_url"])["state"]

    done = await integrated["c"].get("/api/mcp-oauth/callback", params={"state": state, "code": "CODE-1"})

    assert done.status_code == 303
    assert done.headers["location"] == (
        f"{PUBLIC}{web_root}?connector_id={connector_id}&connector_status=connected"
    )


async def test_preset_oauth_callback_still_routes_to_the_preset_handler(integrated):
    preset = await integrated["c"].get("/api/connectors/oauth/google/callback", params={"state": "bogus"})
    assert preset.status_code == 307
    assert preset.headers["location"] == f"{PUBLIC}/?connector_status=error"


async def test_admin_client_api_is_mounted_with_bot_mode_enabled(integrated):
    assert (await integrated["c"].get("/api/admin/mcp-oauth-clients")).status_code == 403


async def test_a_grant_for_a_previous_url_is_not_shown_as_connected(env):
    await _authorized(env)
    await env["connectors"].upsert(
        env["alice"].id, "remote", kind="mcp", transport="http", url="https://mcp.example.com/other"
    )
    (row,) = (await env["c"].get("/api/connectors", headers=ALICE)).json()
    assert row["oauth_grant"]["state"] == "needs_authorization"
    assert row["oauth_grant"]["issuer"] is None
