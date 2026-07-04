"""Telegram linking API (status / link / unlink)."""

from tests.conftest_app import build_api_app, client


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def test_status_when_telegram_disabled(db_factory):
    # build_api_app leaves telegram=None (disabled).
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c)
        r = await c.get("/api/telegram/status", headers=_bearer(token))
        assert r.status_code == 200
        assert r.json() == {"enabled": False, "linked": False, "bot_username": ""}


async def test_link_rejected_when_disabled(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c)
        r = await c.post("/api/telegram/link", headers=_bearer(token))
        assert r.status_code == 400


async def test_link_and_status_when_enabled(db_factory):
    app = build_api_app(db_factory)

    # Simulate an enabled channel with a known bot username.
    class FakeChannel:
        bot_username = "ClawTestBot"

    app.state.claw.telegram = FakeChannel()

    async with client(app) as c:
        token = await _register(c)
        status = (await c.get("/api/telegram/status", headers=_bearer(token))).json()
        assert status["enabled"] is True and status["bot_username"] == "ClawTestBot"

        link = await c.post("/api/telegram/link", headers=_bearer(token))
        assert link.status_code == 200
        body = link.json()
        assert len(body["code"]) == 6 and body["bot_username"] == "ClawTestBot"
        # The code is now valid in the shared link service.
        assert app.state.claw.telegram_link.consume(body["code"]) is not None


async def test_unlink(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c)
        me = (await c.get("/api/auth/me", headers=_bearer(token))).json()
        await app.state.claw.users.set_telegram_id(me["id"], "42")

        status = (await c.get("/api/telegram/status", headers=_bearer(token))).json()
        assert status["linked"] is True

        r = await c.delete("/api/telegram/link", headers=_bearer(token))
        assert r.status_code == 200 and r.json()["linked"] is False
        status = (await c.get("/api/telegram/status", headers=_bearer(token))).json()
        assert status["linked"] is False
