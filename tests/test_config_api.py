"""Heartbeat config + policy toggle + admin capability stats over the API."""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_heartbeat_default_off_then_enable_disable(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c, "a@x.io")

        r = await c.get("/api/heartbeat", headers=_bearer(token))
        assert r.status_code == 200 and r.json()["enabled"] is False

        r = await c.put("/api/heartbeat", json={"interval_minutes": 30}, headers=_bearer(token))
        assert r.status_code == 200
        assert r.json()["enabled"] is True and r.json()["interval_minutes"] == 30
        assert r.json()["next_run_at"] is not None

        # Persisted across requests.
        r = await c.get("/api/heartbeat", headers=_bearer(token))
        assert r.json()["interval_minutes"] == 30

        r = await c.put("/api/heartbeat", json={"interval_minutes": 0}, headers=_bearer(token))
        assert r.json()["enabled"] is False and r.json()["next_run_at"] is None


async def test_heartbeat_rejects_out_of_range(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c, "a@x.io")
        assert (await c.put("/api/heartbeat", json={"interval_minutes": 5000}, headers=_bearer(token))).status_code == 422


async def test_policy_view_and_admin_toggle(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")

        r = await c.get("/api/policy", headers=_bearer(admin))
        assert r.status_code == 200
        assert "email" in [rule["name"] for rule in r.json()["rules"]]

        r = await c.put("/api/policy", json={"monitor_only": True}, headers=_bearer(admin))
        assert r.status_code == 200 and r.json()["monitor_only"] is True


async def test_stats_report_capabilities(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        r = await c.get("/api/admin/stats", headers=_bearer(admin))
        body = r.json()
        assert "browser_enabled" in body and "telegram_enabled" in body
        # Defaults in the test Settings: both off.
        assert body["browser_enabled"] is False and body["telegram_enabled"] is False
