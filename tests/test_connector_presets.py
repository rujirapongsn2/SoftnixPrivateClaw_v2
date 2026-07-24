from claw.core.connector_presets import get_preset, list_presets
from tests.conftest_app import build_api_app, client


def test_catalog_has_expected_connectors():
    keys = {p["key"] for p in list_presets()}
    assert {"github", "gmail", "outlook", "notion", "tavily"}.issubset(keys)
    gh = get_preset("github")
    assert gh.transport == "stdio" and "GITHUB_PERSONAL_ACCESS_TOKEN" in gh.env_fields


def test_unknown_preset_is_none():
    assert get_preset("nope") is None


def test_google_sheets_preset_is_oauth_and_reuses_google_provider():
    preset = get_preset("google-sheets")
    assert preset is not None
    assert preset.transport == "stdio"
    assert preset.setup == "oauth"
    # Same oauth_provider as the existing Gmail preset — reuses the one
    # Control-Plane-registered Google OAuth app, no separate app needed.
    assert preset.oauth_provider == "google"
    assert "https://www.googleapis.com/auth/spreadsheets" in preset.oauth_scopes
    assert preset.env_prefix == "GOOGLE_SHEETS"
    assert preset.command == "python -m claw.integrations.google_sheets_mcp_server"


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def test_presets_endpoint(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c)
        r = await c.get("/api/connectors/presets", headers=_bearer(token))
        assert r.status_code == 200
        labels = {p["label"] for p in r.json()}
        assert "GitHub" in labels and "Tavily Search" in labels
        # Each preset lists the env fields the user must supply.
        gh = next(p for p in r.json() if p["key"] == "github")
        assert gh["env_fields"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]


async def test_create_connector_from_preset_fields(db_factory):
    # Simulates the UI: fetch preset → prefill → save via the normal upsert (admin).
    app = build_api_app(db_factory)
    async with client(app) as c:
        token = await _register(c)  # first user = admin
        preset = get_preset("github")
        r = await c.put(
            f"/api/connectors/{preset.name}",
            headers=_bearer(token),
            json={
                "name": preset.name,
                "transport": preset.transport,
                "command": preset.command,
                "url": "",
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret"},
                "enabled": False,
            },
        )
        assert r.status_code == 200
        listed = await c.get("/api/connectors", headers=_bearer(token))
        gh = next(cn for cn in listed.json() if cn["name"] == "github")
        assert gh["transport"] == "stdio" and "server-github" in gh["command"]
