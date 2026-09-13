import io
import zipfile
import pytest
from claw.skills.bundles import parse_bundle, install_bundle
from claw.db.stores import SkillStore
from claw.tools.skills import ReadSkillTool
from sbot.tools.skills import ReadSkillTool as SbotReader


def bundle(extra=None, name="demo"):
    out = io.BytesIO()
    files = {
        "demo/SKILL.md": f"---\nname: {name}\ndescription: Draw diagrams\n---\nRead [style](references/style.md).",
        "demo/references/style.md": "Use blue",
    }
    files.update(extra or {})
    with zipfile.ZipFile(out, "w") as z:
        for p, c in files.items():
            z.writestr(p, c)
    return out.getvalue()


def test_parse_nested_bundle():
    parsed = parse_bundle(bundle())
    assert parsed["name"] == "demo"
    assert "references/style.md" in parsed["files"]
    assert parsed["metadata"]["size_bytes"] > 0


async def test_import_quota_limits_bundle_count(db_factory, stores, monkeypatch):
    import claw.skills.bundles as bundles

    monkeypatch.setattr(bundles, "MAX_BUNDLES_PER_USER", 1)
    owner = await stores["users"].get_or_create_by_email("bundle-count@test.local")
    store = SkillStore(db_factory)
    await install_bundle(store, owner.id, parse_bundle(bundle()))
    second = parse_bundle(bundle(name="other"))
    with pytest.raises(ValueError, match="up to 1"):
        await install_bundle(store, owner.id, second)


async def test_import_quota_limits_total_bytes(db_factory, stores, monkeypatch):
    import claw.skills.bundles as bundles

    parsed = parse_bundle(bundle())
    monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES_PER_USER", parsed["metadata"]["size_bytes"] - 1)
    owner = await stores["users"].get_or_create_by_email("bundle-bytes@test.local")
    with pytest.raises(ValueError, match="storage limit"):
        await install_bundle(SkillStore(db_factory), owner.id, parsed)


@pytest.mark.parametrize("path", ["../escape.md", "/absolute.md", "demo/../escape.md", "demo\\escape.md"])
def test_reject_escape(path):
    with pytest.raises(ValueError):
        parse_bundle(bundle({path: "x"}))


def test_missing_reference():
    with pytest.raises(ValueError, match="missing reference"):
        parse_bundle(bundle({"demo/references/style.md": "[missing](absent.md)"}))


@pytest.mark.parametrize("reader", [ReadSkillTool, SbotReader])
async def test_resources_follow_subscription_and_immutable_content(db_factory, stores, reader, tmp_path):
    owner = await stores["users"].get_or_create_by_email("bundle-owner@test.local")
    peer = await stores["users"].get_or_create_by_email("bundle-peer@test.local")
    store = SkillStore(db_factory)
    skill = await install_bundle(store, owner.id, parse_bundle(bundle()))
    with pytest.raises(ValueError):
        await install_bundle(store, owner.id, parse_bundle(bundle()))
    with pytest.raises(ValueError, match="read only"):
        await store.upsert(owner.id, "demo", content="changed")
    await store.upsert(owner.id, "demo", visibility="public")
    tool = reader(store, peer.id, workspace=tmp_path)
    assert "Error" in await tool.execute(name="demo", path="references/style.md")
    await store.set_subscription(peer.id, skill.id, True)
    assert "Use blue" in await tool.execute(name="demo", path="references/style.md")
    assert "Resource copied" in await tool.execute(name="demo", path="references/style.md", materialize=True)
    assert "Error" in await tool.execute(name="demo", path="../SKILL.md")
    await store.set_subscription(peer.id, skill.id, False)
    assert "Error" in await tool.execute(name="demo", path="references/style.md")


def test_svg_preview_is_sandbox_document(tmp_path):
    from claw.api.file_preview import preview_html
    from sbot.api.file_preview import preview_html as sbot_preview

    path = tmp_path / "drawing.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text>ไทย</text><script>alert(1)</script></svg>'
    )
    for preview in (preview_html, sbot_preview):
        result = preview(path)
        assert "ไทย" in result["html"]
        assert "default-src 'none'" in result["html"]
        assert not result["truncated"]


@pytest.mark.parametrize("bot_mode", [False, True])
async def test_import_api_and_sharing_roundtrip(db_factory, bot_mode):
    from tests.conftest_app import build_api_app, client
    from tests.test_manage import _register, _bearer

    app = build_api_app(db_factory)
    if bot_mode:
        from sbot.api.manage import router
        from sbot.api.deps import get_state as bs, current_user as bu
        from claw.api.deps import get_state, current_user
        from sbot.db.stores import SkillStore as BS

        app.router.routes = [
            r for r in app.router.routes if not getattr(r, "path", "").startswith("/api/skills")
        ]
        app.include_router(router)
        app.dependency_overrides[bs] = get_state
        app.dependency_overrides[bu] = current_user
        app.state.claw.skills = BS(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "bundle@example.com")
        headers = _bearer(token)
        response = await c.post(
            "/api/skills/import", files={"file": ("skill.zip", bundle(), "application/zip")}, headers=headers
        )
        assert response.status_code == 200, response.text
        skill = response.json()
        assert skill["bundle"]["files"] == ["SKILL.md", "references/style.md"]
        saved = await c.put("/api/skills/demo", json={**skill, "visibility": "public"}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["bundle"] == skill["bundle"]
        assert (
            await c.put("/api/skills/demo", json={**skill, "content": "changed"}, headers=headers)
        ).status_code == 400
        invalid = await c.post(
            "/api/skills/import-github",
            json={"repository": "http://127.0.0.1", "commit": "a" * 40},
            headers=headers,
        )
        assert invalid.status_code == 400


def test_migration_keeps_existing_text_skill():
    import importlib
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module("migrations.versions.e8f9a0b1c2d3_skill_bundles")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE skills (id VARCHAR(32) PRIMARY KEY, content TEXT)"))
        connection.execute(sa.text("INSERT INTO skills VALUES ('old', 'Original instructions')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT content, bundle_id FROM skills")).one() == (
            "Original instructions",
            None,
        )
        assert "skill_bundle_versions" in sa.inspect(connection).get_table_names()
