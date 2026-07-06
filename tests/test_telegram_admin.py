"""Admin-managed Telegram bot config: store + API (self-service, no .env)."""

import pytest

from claw.db.stores import TelegramConfigStore
from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- store


async def test_store_returns_none_when_never_configured(db_factory):
    store = TelegramConfigStore(db_factory)
    assert await store.get() is None
    assert await store.public() == {"has_token": False, "enabled": False}


async def test_store_set_and_get_roundtrip(db_factory):
    store = TelegramConfigStore(db_factory)
    await store.set("123:abc-token", True)
    cfg = await store.get()
    assert cfg == {"bot_token": "123:abc-token", "enabled": True}
    assert await store.public() == {"has_token": True, "enabled": True}


async def test_store_blank_token_preserves_existing(db_factory):
    store = TelegramConfigStore(db_factory)
    await store.set("123:abc-token", True)
    # Only toggling `enabled` (blank token) must not wipe the saved token.
    await store.set("", False)
    cfg = await store.get()
    assert cfg == {"bot_token": "123:abc-token", "enabled": False}


async def test_store_token_encrypted_at_rest(db_factory, tmp_path):
    from claw.security.crypto import SecretBox

    store = TelegramConfigStore(db_factory, secret_box=SecretBox("test-secret-key"))
    await store.set("123:abc-token", True)
    # Encrypted on the way in...
    async with db_factory() as db:
        from claw.db.models import AppSetting

        row = await db.get(AppSetting, "telegram_bot")
        assert row.value["bot_token"] != "123:abc-token"
        assert row.value["bot_token"].startswith("enc::")
    # ...and decrypted transparently on the way out.
    cfg = await store.get()
    assert cfg["bot_token"] == "123:abc-token"


# ---------------------------------------------------------------- admin API


@pytest.fixture
def stub_telegram_lifecycle(monkeypatch):
    """Admin PUT validates + (re)starts a live bot connection — stub both so
    tests never hit the real Telegram API or spin up a background poll loop."""

    async def fake_validate(token):
        if token == "bad-token":
            raise ValueError("Unauthorized")
        return {"username": "ClawTestBot"}

    monkeypatch.setattr("claw.api.admin.validate_bot_token", fake_validate)

    class FakeChannel:
        bot_username = "ClawTestBot"

    async def fake_ensure_running(self, token):
        self.channel = FakeChannel() if token else None
        return self.channel

    async def fake_stop(self):
        self.channel = None

    from claw.channels.telegram import TelegramManager

    monkeypatch.setattr(TelegramManager, "ensure_running", fake_ensure_running)
    monkeypatch.setattr(TelegramManager, "stop", fake_stop)


async def test_admin_sees_not_configured_by_default(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.get("/api/admin/telegram", headers=_bearer(admin_token))
        assert r.status_code == 200
        assert r.json() == {
            "has_token": False, "enabled": False, "source": "none", "running": False, "bot_username": "",
        }


async def test_admin_sees_env_fallback_source(db_factory):
    app = build_api_app(db_factory, telegram_bot_token="env-token-123")
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.get("/api/admin/telegram", headers=_bearer(admin_token))
        body = r.json()
        assert body["source"] == "env" and body["has_token"] is True and body["enabled"] is True


async def test_admin_saves_token_and_connects_immediately(db_factory, stub_telegram_lifecycle):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.put(
            "/api/admin/telegram",
            json={"bot_token": "123:good-token", "enabled": True},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["has_token"] is True
        assert body["enabled"] is True
        assert body["source"] == "database"
        assert body["running"] is True
        assert body["bot_username"] == "ClawTestBot"
        # The live channel is actually wired onto AppState, not just reported.
        assert app.state.claw.telegram is not None


async def test_admin_rejects_bad_token(db_factory, stub_telegram_lifecycle):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.put(
            "/api/admin/telegram",
            json={"bot_token": "bad-token", "enabled": True},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 422
        # Rejected token must not be persisted.
        r2 = await c.get("/api/admin/telegram", headers=_bearer(admin_token))
        assert r2.json()["has_token"] is False


async def test_admin_requires_token_to_enable_first_time(db_factory, stub_telegram_lifecycle):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.put(
            "/api/admin/telegram", json={"bot_token": "", "enabled": True}, headers=_bearer(admin_token)
        )
        assert r.status_code == 422


async def test_admin_can_disable_without_losing_token(db_factory, stub_telegram_lifecycle):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await c.put(
            "/api/admin/telegram",
            json={"bot_token": "123:good-token", "enabled": True},
            headers=_bearer(admin_token),
        )
        r = await c.put(
            "/api/admin/telegram", json={"bot_token": "", "enabled": False}, headers=_bearer(admin_token)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["has_token"] is True  # token retained
        assert body["enabled"] is False
        assert body["running"] is False  # but the live channel is stopped
        assert app.state.claw.telegram is None


async def test_non_admin_denied(db_factory, stub_telegram_lifecycle):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")
        assert (await c.get("/api/admin/telegram", headers=_bearer(user_token))).status_code == 403
        assert (
            await c.put(
                "/api/admin/telegram", json={"bot_token": "x", "enabled": True}, headers=_bearer(user_token)
            )
        ).status_code == 403


# ---------------------------------------------------------------- TelegramManager (dynamic reload)


async def test_manager_reuses_channel_for_same_token(monkeypatch):
    from claw.channels.telegram import TelegramManager

    class FakeTransport:
        async def get_updates(self, offset, timeout):
            import asyncio

            await asyncio.sleep(0.05)
            return []

        async def send_message(self, chat_id, text):
            pass

        async def get_me(self):
            return {"username": "ClawTestBot"}

    monkeypatch.setattr("claw.channels.telegram.HttpTelegramTransport", lambda token: FakeTransport())

    mgr = TelegramManager(None, None, None, None)
    try:
        first = await mgr.ensure_running("token-a")
        again = await mgr.ensure_running("token-a")
        assert first is again  # same token — no restart

        changed = await mgr.ensure_running("token-b")
        assert changed is not first  # different token — restarted

        stopped = await mgr.ensure_running("")
        assert stopped is None
        assert mgr.channel is None
    finally:
        await mgr.stop()
