"""Production composition, not a second fake app: mode isolation and shared config."""

import pytest
import httpx
from claw.main import create_app
from claw.config import Settings
from claw.db.models import Base
from claw.auth.tokens import create_access_token
from tests.conftest import FakeProvider, text_turn


@pytest.fixture
async def integrated(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/mode.db",
        secret_key="test-mode-secret" * 3,
        auto_migrate=False,
        workspaces_root=tmp_path / "workspaces",
        knowledge_root=tmp_path / "knowledge",
        branding_root=tmp_path / "branding",
        blueprints_root=tmp_path / "blueprints",
        sandbox={"enabled": False},
        llm={"api_key": "test-key"},
    )
    app = create_app(settings)
    host, mode = app.state.claw, app.state.sbot
    engine = host.users.factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    user = await host.users.get_or_create_by_email("mode@example.test")
    token = create_access_token(user.id, settings.secret_key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield app, client, user
    await host.runtime.drain()
    await mode.runtime.drain()
    await engine.dispose()


async def test_mode_session_and_file_authorization(integrated):
    app, c, u = integrated
    a = (await c.post("/api/sessions", json={})).json()["id"]
    b = (await c.post("/modes/sbot/api/sessions", json={})).json()["id"]
    assert a != b
    assert [s["id"] for s in (await c.get("/api/sessions")).json()] == [a]
    assert [s["id"] for s in (await c.get("/modes/sbot/api/sessions")).json()] == [b]
    for prefix, foreign in [("/api", b), ("/modes/sbot/api", a)]:
        assert (await c.get(f"{prefix}/sessions/{foreign}/messages")).status_code == 404
        assert (await c.delete(f"{prefix}/sessions/{foreign}")).status_code == 404
    for state, marker in [(app.state.claw, "private"), (app.state.sbot, "team")]:
        directory = state.settings.workspaces_root / u.id
        directory.mkdir(parents=True)
        (directory / "report.txt").write_text(marker)
    assert (await c.get(f"/api/sessions/{a}/files/report.txt")).text == "private"
    assert (await c.get(f"/modes/sbot/api/sessions/{b}/files/report.txt")).text == "team"


async def test_sbot_bot_api_enforces_active_team_quota(integrated):
    from sbot.db.stores import MAX_BOTS_PER_OWNER

    app, c, user = integrated
    assert (await c.get("/modes/sbot/api/bots")).status_code == 200  # Seeds the Team Lead.
    for index in range(MAX_BOTS_PER_OWNER - 1):
        await app.state.sbot.bots.create(owner_id=user.id, name=f"Specialist {index}")

    response = await c.post("/modes/sbot/api/bots", json={"name": "One too many"})
    assert response.status_code == 409
    assert f"maximum of {MAX_BOTS_PER_OWNER} bots" in response.json()["detail"]


async def test_blueprint_routes_share_library_and_use_host_workspace(integrated):
    app, c, u = integrated
    created = await c.post("/modes/sbot/api/blueprints", data={"name": "Template"},
                           files={"file": ("template.docx", b"template bytes")})
    assert created.status_code == 200, created.text
    bp = created.json()
    assert (await c.get("/api/blueprints")).json()[0]["id"] == bp["id"]
    materialized = await c.post(f"/api/blueprints/{bp['id']}/materialize", json={})
    assert materialized.status_code == 200, materialized.text
    ref = materialized.json()
    host = app.state.claw
    workspace = host.settings.workspaces_root / u.id
    copy = workspace / ref["path"]
    assert copy.read_bytes() == b"template bytes"
    assert not (app.state.sbot.settings.workspaces_root / u.id / ref["path"]).exists()
    from claw.core.context import build_user_content
    content, _ = build_user_content("Fill the template", [str(copy)], workspace)
    assert "[Blueprint templates]" in content
    tool = host.runtime.get_agent(u.id).tools.get("save_blueprint")
    assert tool is not None
    assert "Saved Blueprint" in await tool.execute(path=ref["path"], name="Saved in PrivateClaw")
    assert len((await c.get("/modes/sbot/api/blueprints")).json()) == 2


async def test_core_and_search_history_isolation(integrated):
    app, c, u = integrated
    a, b = app.state.claw, app.state.sbot
    await a.memories.set_core(u.id, "PRIVATE_ONLY")
    await b.memories.set_core(u.id, "SBOT_ONLY")
    await a.memories.append_history(u.id, "private history")
    await b.memories.append_history(u.id, "sbot history")
    assert (await c.get("/api/memory")).json()["core"] == "PRIVATE_ONLY"
    assert (await c.get("/modes/sbot/api/memory")).json()["core"] == "SBOT_ONLY"
    assert await a.memories.recent_history(u.id) == ["private history"]
    assert await b.memories.recent_history(u.id) == ["sbot history"]


async def test_shared_services_and_both_runtime_turns(integrated):
    app, c, u = integrated
    a, b = app.state.claw, app.state.sbot
    for name in ["users", "groups", "policy", "llm_config", "connectors_mgr", "skills", "usage", "branding"]:
        assert getattr(a, name) is getattr(b, name)
    provider = FakeProvider([text_turn("private answer"), text_turn("team answer")])
    for state in [a, b]:
        state.runtime.provider = provider
        state.runtime.memory.provider = provider
    sa = await a.sessions.create(u.id)
    sb = await b.sessions.create(u.id)
    assert await a.runtime.handle_message(u.id, sa.id, "PRIVATE_SENTINEL_984") == "private answer"
    assert await b.runtime.handle_message(u.id, sb.id, "second") == "team answer"
    assert "PRIVATE_SENTINEL_984" not in str(provider.calls[-1])
    assert len(await a.messages.recent(sa.id)) == 2
    assert len(await b.messages.recent(sb.id)) == 2


def test_additive_migration_roundtrip_preserves_legacy(tmp_path):
    import importlib.util
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pathlib import Path

    legacy = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if not table.name.startswith("sbot_"):
            copied = table.to_metadata(legacy)
            for column in list(copied.columns):
                if column.name.startswith(("sbot_", "project_container")):
                    copied._columns.remove(column)
    engine = sa.create_engine(f"sqlite:///{tmp_path}/upgrade.db")
    legacy.create_all(engine)
    path = Path("migrations/versions/f0a1b2c3d4e5_sbot_mode.py")
    spec = importlib.util.spec_from_file_location("mode_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as conn:
        conn.execute(
            legacy.tables["users"].insert().values(id="owner", email="legacy@test", display_name="Original")
        )
        conn.execute(
            legacy.tables["sessions"].insert().values(id="old-session", user_id="owner", title="Preserve")
        )
        conn.execute(
            legacy.tables["memories"].insert().values(user_id="owner", kind="core", content="Legacy memory")
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert conn.scalar(sa.text("SELECT title FROM sessions")) == "Preserve"
        assert conn.scalar(sa.text("SELECT content FROM memories")) == "Legacy memory"
        assert conn.scalar(sa.text("SELECT count(*) FROM sbot_sessions")) == 0
        assert conn.scalar(sa.text("SELECT count(*) FROM sbot_memories")) == 0
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert conn.scalar(sa.text("SELECT title FROM sessions")) == "Preserve"
        assert "sbot_sessions" not in sa.inspect(conn).get_table_names()
    engine.dispose()


async def test_schedules_heartbeat_and_user_delete(integrated):
    app, c, u = integrated
    a, b = app.state.claw, app.state.sbot
    await c.put("/api/heartbeat", json={"interval_minutes": 30})
    await c.put("/modes/sbot/api/heartbeat", json={"interval_minutes": 60})
    assert (await c.get("/api/heartbeat")).json()["interval_minutes"] == 30
    assert (await c.get("/modes/sbot/api/heartbeat")).json()["interval_minutes"] == 60
    sa = await a.sessions.create(u.id)
    sb = await b.sessions.create(u.id)
    await a.messages.append(sa.id, [{"role": "user", "content": "private"}])
    await b.messages.append(sb.id, [{"role": "user", "content": "team"}])
    bot = await b.bots.get_or_create_cos(u.id)
    await b.memories.set_core(u.id, "bot memory", scope="bot", scope_id=bot.id)
    assert await a.users.delete(u.id)
    assert await a.sessions.get(sa.id) is None
    assert await b.sessions.get(sb.id) is None


def test_rejects_overlapping_workspaces(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "data",
        sbot_workspaces_root=tmp_path / "data" / "nested",
    )
    with pytest.raises(ValueError, match="must not overlap"):
        create_app(settings)


def test_mode_can_be_disabled(tmp_path):
    app = create_app(
        Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:", sbot_enabled=False)
    )
    assert not hasattr(app.state, "sbot")
    assert all(getattr(route, "path", None) != "/modes/sbot" for route in app.routes)


async def test_admin_reports_count_both_modes(integrated):
    app, c, u = integrated
    await app.state.claw.users.update_flags(u.id, is_admin=True)
    await app.state.claw.sessions.create(u.id)
    await app.state.sbot.sessions.create(u.id)
    response = await c.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    assert response.json()["stats"]["sessions"] == 2
    assert response.json()["stats"]["active_users"] == 1
