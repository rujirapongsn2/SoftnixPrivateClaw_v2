"""Admin-global "Pre-built Connectors": an admin provisions an MCP connector
once (owner_id=None) and every user gets it with no per-user setup — see
claw/api/connector_shared.py and claw/core/connectors.py's shared pool.
"""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_admin_connectors_require_admin(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _register(c, "admin@x.io")  # first user = implicit admin
        user_token, _ = await _register(c, "normal@x.io")

        assert (await c.get("/api/admin/connectors", headers=_bearer(user_token))).status_code == 403
        assert (
            await c.put(
                "/api/admin/connectors/search",
                json={"name": "search", "transport": "http", "url": "https://example.invalid/mcp"},
                headers=_bearer(user_token),
            )
        ).status_code == 403
        assert (
            await c.delete("/api/admin/connectors/nonexistent", headers=_bearer(user_token))
        ).status_code == 403


async def test_admin_connector_crud_roundtrip(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")

        created = await c.put(
            "/api/admin/connectors/search",
            json={
                "name": "search",
                "transport": "http",
                "url": "https://example.invalid/mcp",
                "env": {"HEADER_Authorization": "Bearer secret123"},
                "enabled": True,
            },
            headers=_bearer(admin_token),
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["name"] == "search"
        # The admin, as the connector's own scope owner (owner_id=None, and
        # the caller IS an admin), sees the full detail including secrets —
        # same as a regular user always seeing their own personal connector's
        # secrets today.
        assert body["env"] == {"HEADER_Authorization": "Bearer secret123"}

        listed = await c.get("/api/admin/connectors", headers=_bearer(admin_token))
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()] == ["search"]

        deleted = await c.delete(f"/api/admin/connectors/{body['id']}", headers=_bearer(admin_token))
        assert deleted.status_code == 200 and deleted.json()["deleted"] is True
        assert (await c.get("/api/admin/connectors", headers=_bearer(admin_token))).json() == []


async def test_admin_connector_allows_arbitrary_stdio_command(db_factory):
    """Only admins may supply an arbitrary (non-preset) stdio command — this
    is the same restriction personal connectors have (claw/api/manage.py),
    just always satisfied here since these routes are require_admin-gated."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")

        r = await c.put(
            "/api/admin/connectors/custom",
            json={
                "name": "custom",
                "transport": "stdio",
                "command": "node /opt/custom-mcp-server.js",
                "enabled": True,
            },
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["command"] == "node /opt/custom-mcp-server.js"


async def test_connectors_global_endpoint_redacts_secrets_and_is_visible_to_any_user(db_factory):
    """A regular, non-admin, non-owner user must be able to see that a global
    connector exists (name/description/transport/enabled/runtime) but must
    NEVER receive its command/url/env — those may hold secrets."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        r = await c.put(
            "/api/admin/connectors/search",
            json={
                "name": "search",
                "description": "Web search",
                "transport": "http",
                "url": "https://example.invalid/mcp?apikey=SUPERSECRET",
                "env": {"HEADER_Authorization": "Bearer secret123"},
                "enabled": True,
            },
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text

        seen = await c.get("/api/connectors/global", headers=_bearer(user_token))
        assert seen.status_code == 200
        rows = seen.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "search"
        assert row["description"] == "Web search"
        assert row["transport"] == "http"
        assert row["enabled"] is True
        assert "command" not in row
        assert "url" not in row
        assert "env" not in row
        # Belt-and-suspenders: the secret string itself must not appear
        # anywhere in the serialized response.
        assert "SUPERSECRET" not in seen.text
        assert "secret123" not in seen.text


async def test_connectors_global_endpoint_empty_when_none_provisioned(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        _, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        r = await c.get("/api/connectors/global", headers=_bearer(user_token))
        assert r.status_code == 200
        assert r.json() == []


async def test_personal_connectors_list_excludes_global_ones(db_factory):
    """GET /api/connectors (personal) must stay scoped to the caller's own
    connectors — the global list is a separate, deliberately distinct
    endpoint (/api/connectors/global), not merged into this one."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        await c.put(
            "/api/admin/connectors/search",
            json={"name": "search", "transport": "http", "url": "https://example.invalid/mcp", "enabled": True},
            headers=_bearer(admin_token),
        )

        mine = await c.get("/api/connectors", headers=_bearer(user_token))
        assert mine.status_code == 200
        assert mine.json() == []
