"""The OAuth quick-install path writes connectors through ConnectorStore.upsert
directly, not through claw/api/connector_shared.py's upsert_connector — so the
kind lock enforced there doesn't cover it. These tests pin the guard that keeps
an OAuth preset from being installed over a user's own same-named kind="api"
connector (which would leave a half-REST/half-MCP row behind).
"""

from claw.api.connector_oauth import router as oauth_router
from claw.api.connector_shared import connector_row
from claw.auth import connector_oauth as flow
from claw.core.connector_presets import get_preset
from tests.conftest_app import build_api_app, client


def _app(db_factory):
    app = build_api_app(db_factory)
    app.include_router(oauth_router)
    return app


async def _user(app, email="oauthkind@x.io"):
    return await app.state.claw.users.create(email=email, password_hash="h")


def _auth(email: str) -> dict[str, str]:
    return {"Authorization": "Bearer t", "X-User-Email": email}


def _patch_exchange(monkeypatch):
    import claw.api.connector_oauth as mod

    async def fake_exchange(preset, app, code, redirect, http):
        return {"access_token": f"access-{code}", "refresh_token": f"refresh-{code}"}

    monkeypatch.setattr(mod.flow, "exchange_code", fake_exchange)


async def test_oauth_install_blocked_when_user_has_same_named_api_connector(db_factory, monkeypatch):
    app = _app(db_factory)
    state_app = app.state.claw
    user = await _user(app)
    preset = get_preset("gmail")
    await state_app.oauth_apps.set("google", client_id="cid", client_secret="csec")
    _patch_exchange(monkeypatch)

    operations = [
        {"name": "list_rows", "method": "GET", "path": "/rows", "description": "", "parameters": []}
    ]
    await state_app.connectors.upsert(
        user.id,
        preset.name,
        kind="api",
        transport="http",
        url="https://my-own-api.example.com",
        operations=operations,
        enabled=True,
    )

    token = flow.make_state(user.id, preset.key, preset.oauth_provider, state_app.settings.secret_key)
    async with client(app) as c:
        r = await c.get(
            f"/api/connectors/oauth/{preset.oauth_provider}/callback",
            params={"code": "abc", "state": token},
        )
    assert r.status_code == 307
    assert "connector_status=name_conflict" in r.headers["location"]

    # The user's own connector must be completely untouched — same kind, same
    # base url, same operations, and no OAuth token written into its env.
    row = await state_app.connectors.get_by_name(user.id, preset.name)
    assert row.kind == "api"
    assert row.url == "https://my-own-api.example.com"
    assert row.operations == operations
    assert "access-abc" not in str(row.env)


async def test_oauth_install_still_works_for_a_normal_mcp_connector(db_factory, monkeypatch):
    """Regression guard: the new check must not break the ordinary install or
    the re-run/token-refresh path."""
    app = _app(db_factory)
    state_app = app.state.claw
    user = await _user(app, email="oauthok@x.io")
    preset = get_preset("gmail")
    await state_app.oauth_apps.set("google", client_id="cid", client_secret="csec")
    _patch_exchange(monkeypatch)

    token = flow.make_state(user.id, preset.key, preset.oauth_provider, state_app.settings.secret_key)
    async with client(app) as c:
        r = await c.get(
            f"/api/connectors/oauth/{preset.oauth_provider}/callback",
            params={"code": "abc", "state": token},
        )
        assert "connector_status=connected" in r.headers["location"]

        row = await state_app.connectors.get_by_name(user.id, preset.name)
        assert row.kind == "mcp"
        assert row.operations is None
        assert row.env["GMAIL_REFRESH_TOKEN"] == "refresh-abc"

        # Re-running the flow (token refresh) stays allowed.
        r = await c.get(
            f"/api/connectors/oauth/{preset.oauth_provider}/callback",
            params={"code": "abc2", "state": token},
        )
        assert "connector_status=connected" in r.headers["location"]
        row = await state_app.connectors.get_by_name(user.id, preset.name)
        assert row.env["GMAIL_TOKEN"] == "access-abc2"
        assert row.env["GMAIL_REFRESH_TOKEN"] == "refresh-abc2"


async def test_disconnect_removes_oauth_preset_and_is_idempotent(db_factory, monkeypatch):
    app = _app(db_factory)
    state_app = app.state.claw
    email = "disconnect@x.io"
    user = await _user(app, email=email)
    preset = get_preset("gmail")
    await state_app.connectors.upsert(
        user.id,
        preset.name,
        kind="mcp",
        transport=preset.transport,
        command=preset.command,
        url=preset.url,
        env={"GMAIL_TOKEN": "old", "GMAIL_REFRESH_TOKEN": "expired"},
        enabled=True,
    )

    disconnected: list[str] = []

    async def fake_disconnect_user(user_id: str):
        disconnected.append(user_id)

    monkeypatch.setattr(state_app.connectors_mgr, "disconnect_user", fake_disconnect_user)
    async with client(app) as c:
        r = await c.delete("/api/connectors/oauth/gmail/disconnect", headers=_auth(email))
        assert r.status_code == 200
        assert r.json() == {"disconnected": True}
        assert await state_app.connectors.get_by_name(user.id, preset.name) is None
        assert disconnected == [user.id]

        # Retrying after a lost response remains safe and does not close twice.
        r = await c.delete("/api/connectors/oauth/gmail/disconnect", headers=_auth(email))
        assert r.status_code == 200
        assert r.json() == {"disconnected": True}
        assert disconnected == [user.id]


async def test_disconnect_accepts_legacy_sbot_oauth_command(db_factory, monkeypatch):
    app = _app(db_factory)
    state_app = app.state.claw
    email = "legacy-oauth@x.io"
    user = await _user(app, email=email)
    preset = get_preset("gmail")
    await state_app.connectors.upsert(
        user.id,
        preset.name,
        kind="mcp",
        transport=preset.transport,
        command=preset.command.replace("claw.integrations.", "sbot.integrations."),
        env={"GMAIL_TOKEN": "old", "GMAIL_REFRESH_TOKEN": "expired"},
        enabled=True,
    )
    monkeypatch.setattr(state_app.connectors_mgr, "disconnect_user", lambda _user_id: _async_none())

    async with client(app) as c:
        response = await c.delete("/api/connectors/oauth/gmail/disconnect", headers=_auth(email))
    assert response.status_code == 200
    assert await state_app.connectors.get_by_name(user.id, preset.name) is None


async def _async_none():
    return None


async def test_disconnect_refuses_to_delete_a_newer_oauth_revision(db_factory, monkeypatch):
    app = _app(db_factory)
    state_app = app.state.claw
    email = "oauth-race@x.io"
    user = await _user(app, email=email)
    preset = get_preset("gmail")
    await state_app.connectors.upsert(
        user.id, preset.name, kind="mcp", transport=preset.transport,
        command=preset.command, env={"GMAIL_TOKEN": "old"}, enabled=True,
    )

    async def changed_after_read(owner_id, connector_id, expected_updated_at):
        await state_app.connectors.upsert(owner_id, preset.name, env={"GMAIL_TOKEN": "fresh"})
        return False

    monkeypatch.setattr(state_app.connectors, "delete_if_unchanged", changed_after_read)
    async with client(app) as c:
        response = await c.delete("/api/connectors/oauth/gmail/disconnect", headers=_auth(email))
    assert response.status_code == 409
    remaining = await state_app.connectors.get_by_name(user.id, preset.name)
    assert remaining.env["GMAIL_TOKEN"] == "fresh"


async def test_disconnect_never_deletes_custom_same_name_connector(db_factory):
    app = _app(db_factory)
    state_app = app.state.claw
    email = "custom-gmail@x.io"
    user = await _user(app, email=email)
    preset = get_preset("gmail")
    custom = await state_app.connectors.upsert(
        user.id,
        preset.name,
        kind="api",
        transport="http",
        url="https://example.com",
        operations=[],
        enabled=True,
    )

    async with client(app) as c:
        r = await c.delete("/api/connectors/oauth/gmail/disconnect", headers=_auth(email))
    assert r.status_code == 409
    remaining = await state_app.connectors.get_by_name(user.id, preset.name)
    assert remaining is not None
    assert remaining.id == custom.id


async def test_disconnect_rejects_non_oauth_preset(db_factory):
    app = _app(db_factory)
    email = "disconnect-github@x.io"
    await _user(app, email=email)
    async with client(app) as c:
        r = await c.delete("/api/connectors/oauth/github/disconnect", headers=_auth(email))
    assert r.status_code == 404


async def test_oauth_connector_response_redacts_tokens_and_exposes_safe_metadata(db_factory):
    store_app = _app(db_factory).state.claw
    user = await store_app.users.create(email="redact-oauth@x.io", password_hash="h")
    preset = get_preset("gmail")
    connector = await store_app.connectors.upsert(
        user.id, preset.name, kind="mcp", transport=preset.transport,
        command=preset.command, env={"GMAIL_TOKEN": "access-secret", "GMAIL_REFRESH_TOKEN": "refresh-secret"},
        enabled=True,
    )
    payload = connector_row(connector)
    assert payload["env"] == {}
    assert payload["oauth"] == {
        "preset_key": "gmail", "provider": "google", "has_refresh_token": True,
    }
    assert "secret" not in str(payload)
