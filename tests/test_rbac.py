"""Authorization: per-user own resources vs. is_admin-gated system endpoints."""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_any_user_manages_own_skills(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        _admin_token, _ = await _register(c, "admin@x.io")  # first = admin
        token, user = await _register(c, "normal@x.io")  # second = non-admin
        assert user["is_admin"] is False

        body = {"name": "s", "description": "", "content": "x", "enabled": True}
        r = await c.put("/api/skills/s", json=body, headers=_bearer(token))
        assert r.status_code == 200  # own resource — allowed without admin
        r = await c.get("/api/skills", headers=_bearer(token))
        assert r.status_code == 200
        # The list also includes read-only built-in skills; the user's own skill
        # is the single non-built-in entry.
        own = [s for s in r.json() if not s.get("builtin")]
        assert len(own) == 1 and own[0]["name"] == "s"


async def test_any_user_manages_own_connectors(db_factory):
    """Connectors live under Settings (self-service, like skills/knowledge/
    memory) — a non-admin user connects their own apps without needing an
    admin. Only Control Plane-level config (e.g. /policy below) requires
    is_admin."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        _admin_token, _ = await _register(c, "admin@x.io")  # first = admin
        user_token, _ = await _register(c, "normal@x.io")  # second = non-admin

        conn = {"name": "gh", "transport": "http", "url": "https://mcp.x/mcp", "env": {}, "enabled": True}
        r = await c.put("/api/connectors/gh", json=conn, headers=_bearer(user_token))
        assert r.status_code == 200  # own resource — allowed without admin

        r = await c.delete(f"/api/connectors/{r.json()['id']}", headers=_bearer(user_token))
        assert r.status_code == 200


async def test_non_admin_cannot_set_arbitrary_stdio_connector_command(db_factory):
    """stdio spawns a real, unsandboxed subprocess on the host (see
    ConnectorManager._connect) — a non-admin must not be able to supply their
    own command string, only the fixed built-in preset commands (which never
    come from user input)."""
    from claw.core.connector_presets import get_preset

    app = build_api_app(db_factory)
    async with client(app) as c:
        _admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        arbitrary = {
            "name": "evil",
            "transport": "stdio",
            "command": "/bin/sh -c 'echo pwned'",
            "env": {},
            "enabled": True,
        }
        r = await c.put("/api/connectors/evil", json=arbitrary, headers=_bearer(user_token))
        assert r.status_code == 403

        # The exact command of a built-in stdio preset IS allowed — it's
        # developer-authored code, not user input, even though it's typed
        # into the same field.
        github = get_preset("github")
        preset_body = {
            "name": "github",
            "transport": "stdio",
            "command": github.command,
            "env": {},
            "enabled": True,
        }
        r2 = await c.put("/api/connectors/github", json=preset_body, headers=_bearer(user_token))
        assert r2.status_code == 200, r2.text


async def test_admin_can_set_arbitrary_stdio_connector_command(db_factory):
    """Admins already had unrestricted connector control before connectors
    became self-service; the stdio allowlist must not regress that."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")  # first = admin

        conn = {
            "name": "custom-internal",
            "transport": "stdio",
            "command": "node /opt/internal-mcp-server/index.js",
            "env": {},
            "enabled": True,
        }
        r = await c.put("/api/connectors/custom-internal", json=conn, headers=_bearer(admin_token))
        assert r.status_code == 200, r.text


async def test_policy_requires_admin(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        assert (await c.put("/api/policy", json={"monitor_only": True}, headers=_bearer(user_token))).status_code == 403
        assert (await c.put("/api/policy", json={"monitor_only": True}, headers=_bearer(admin_token))).status_code == 200


async def test_unauthenticated_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        assert (await c.get("/api/skills")).status_code == 401
        assert (await c.get("/api/skills", headers=_bearer("garbage.token.here"))).status_code == 401


async def test_dev_token_fallback_when_enabled(db_factory):
    # auth_mode defaults to "dev" so token+email still works for scripts/tests.
    app = build_api_app(db_factory)
    async with client(app) as c:
        r = await c.get("/api/skills", params={"token": "t", "email": "script@x.io"})
        assert r.status_code == 200
