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


async def test_connectors_and_policy_require_admin(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")

        conn = {"name": "gh", "transport": "http", "url": "https://mcp.x/mcp", "env": {}, "enabled": True}
        assert (await c.put("/api/connectors/gh", json=conn, headers=_bearer(user_token))).status_code == 403
        assert (await c.put("/api/connectors/gh", json=conn, headers=_bearer(admin_token))).status_code == 200

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
