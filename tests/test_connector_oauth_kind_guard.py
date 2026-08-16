"""The OAuth quick-install path writes connectors through ConnectorStore.upsert
directly, not through claw/api/connector_shared.py's upsert_connector — so the
kind lock enforced there doesn't cover it. These tests pin the guard that keeps
an OAuth preset from being installed over a user's own same-named kind="api"
connector (which would leave a half-REST/half-MCP row behind).
"""

from claw.api.connector_oauth import router as oauth_router
from claw.auth import connector_oauth as flow
from claw.core.connector_presets import get_preset
from tests.conftest_app import build_api_app, client


def _app(db_factory):
    app = build_api_app(db_factory)
    app.include_router(oauth_router)
    return app


async def _user(app, email="oauthkind@x.io"):
    return await app.state.claw.users.create(email=email, password_hash="h")


def _patch_exchange(monkeypatch):
    import claw.api.connector_oauth as mod

    async def fake_exchange(preset, app, code, redirect, http):
        return {"access_token": "real-oauth-token", "refresh_token": "r"}

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
    assert "real-oauth-token" not in str(row.env)


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

        # Re-running the flow (token refresh) stays allowed.
        r = await c.get(
            f"/api/connectors/oauth/{preset.oauth_provider}/callback",
            params={"code": "abc2", "state": token},
        )
        assert "connector_status=connected" in r.headers["location"]
