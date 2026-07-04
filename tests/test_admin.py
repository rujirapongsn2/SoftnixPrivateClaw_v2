"""Admin multi-tenant API."""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_admin_lists_and_creates_users(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await _register(c, "u1@x.io")

        r = await c.get("/api/admin/users", headers=_bearer(admin_token))
        assert r.status_code == 200
        assert {u["email"] for u in r.json()} == {"admin@x.io", "u1@x.io"}

        created = await c.post(
            "/api/admin/users",
            json={"email": "u2@x.io", "password": "password123", "is_admin": True},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 200 and created.json()["is_admin"] is True


async def test_non_admin_denied(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")
        assert (await c.get("/api/admin/users", headers=_bearer(user_token))).status_code == 403


async def test_admin_suspends_user_who_then_cannot_act(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        victim_token, victim = await _register(c, "victim@x.io")

        # Victim works before suspension.
        assert (await c.get("/api/skills", headers=_bearer(victim_token))).status_code == 200

        r = await c.patch(
            f"/api/admin/users/{victim['id']}",
            json={"is_active": False},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200 and r.json()["is_active"] is False

        # Suspended → 403 even with a still-valid token.
        assert (await c.get("/api/skills", headers=_bearer(victim_token))).status_code == 403


async def test_admin_promotes_user(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "normal@x.io")

        await c.patch(
            f"/api/admin/users/{user['id']}", json={"is_admin": True}, headers=_bearer(admin_token)
        )
        # Now the promoted user can reach admin endpoints.
        assert (await c.get("/api/admin/users", headers=_bearer(user_token))).status_code == 200


async def test_admin_cannot_demote_self(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, admin = await _register(c, "admin@x.io")
        r = await c.patch(
            f"/api/admin/users/{admin['id']}", json={"is_admin": False}, headers=_bearer(admin_token)
        )
        assert r.status_code == 400


async def test_stats(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await _register(c, "u1@x.io")
        r = await c.get("/api/admin/stats", headers=_bearer(admin_token))
        assert r.status_code == 200
        body = r.json()
        assert body["users"] == 2 and body["admins"] == 1
