import io
import stat
import zipfile
import pytest
from sqlalchemy import select
from claw.skills.bundles import parse_bundle, install_bundle, reference_warnings
from claw.skills.workspace import (
    archive_owned_directory,
    delete_managed_orphan,
    delete_skill_with_workspace,
    find_orphans,
    record_managed_directory,
)
from claw.db.models import Skill, SkillBundleVersion
from claw.db.stores import SkillStore
from claw.tools.skills import ManageSkillTool, ReadSkillTool
from sbot.tools.skills import ManageSkillTool as SbotManager, ReadSkillTool as SbotReader


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


def test_reject_symlink():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("demo/SKILL.md", "---\nname: demo\ndescription: Demo\n---\n")
        link = zipfile.ZipInfo("demo/references/link.md")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError, match="Symlinks"):
        parse_bundle(out.getvalue())


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


@pytest.mark.parametrize("manager", [ManageSkillTool, SbotManager])
async def test_agent_github_import_uses_bundle_pipeline(db_factory, stores, monkeypatch, tmp_path, manager):
    owner = await stores["users"].get_or_create_by_email(f"agent-import-{manager.__module__}@test.local")
    store = SkillStore(db_factory)

    async def fake_download(repository, commit):
        assert repository == "https://github.com/example/demo"
        assert commit == "a" * 40
        return bundle()

    monkeypatch.setattr("claw.skills.github.download_bundle", fake_download)
    tool = manager(store, owner.id, workspace=tmp_path)
    result = await tool.execute(
        action="import_github",
        repository="https://github.com/example/demo",
        commit="a" * 40,
    )
    assert "imported" in result
    skill = await store.get_by_name(owner.id, "demo")
    assert skill is not None and skill.bundle_id
    async with db_factory() as db:
        version = await db.get(SkillBundleVersion, skill.bundle_id)
        assert version is not None and version.skill_id == skill.id
    assert "Use blue" in await ReadSkillTool(store, owner.id).execute(
        name="demo", path="references/style.md"
    )
    assert not (tmp_path / "skills" / "demo").exists()
    duplicate = await tool.execute(
        action="import_github",
        repository="https://github.com/example/demo",
        commit="a" * 40,
    )
    assert "already exists" in duplicate


async def test_bundle_delete_cascades_and_does_not_delete_unowned_workspace(db_factory, stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("bundle-delete@test.local")
    store = SkillStore(db_factory)
    skill = await install_bundle(store, owner.id, parse_bundle(bundle()))
    arbitrary = tmp_path / "skills" / skill.name
    arbitrary.mkdir(parents=True)
    (arbitrary / "user.txt").write_text("keep", encoding="utf-8")

    result = await ManageSkillTool(store, owner.id, workspace=tmp_path).execute(
        action="delete", name=skill.name
    )
    assert result == "Skill 'demo' deleted."
    assert arbitrary.joinpath("user.txt").read_text(encoding="utf-8") == "keep"
    assert "not found" in await ReadSkillTool(store, owner.id).execute(name="demo")
    assert "not found" in await ReadSkillTool(store, owner.id).execute(
        name="demo", path="references/style.md"
    )
    async with db_factory() as db:
        assert await db.scalar(select(Skill).where(Skill.id == skill.id)) is None
        assert await db.scalar(select(SkillBundleVersion).where(SkillBundleVersion.skill_id == skill.id)) is None


async def test_delete_archives_only_matching_managed_directory(db_factory, stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("managed-delete@test.local")
    store = SkillStore(db_factory)
    skill = await store.upsert(owner.id, "managed", content="instructions")
    directory = tmp_path / "skills" / skill.name
    directory.mkdir(parents=True)
    record_managed_directory(tmp_path, owner.id, skill.id, skill.name)
    (directory / "asset.txt").write_text("data", encoding="utf-8")

    result = await ManageSkillTool(store, owner.id, workspace=tmp_path).execute(
        action="delete", name=skill.name
    )
    assert "archived" in result
    assert not directory.exists()
    assert any(path.joinpath("asset.txt").exists() for path in (tmp_path / ".skill-archive").iterdir())


async def test_forged_workspace_manifest_is_not_ownership_evidence(db_factory, stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("wrong-owner@test.local")
    skill = await SkillStore(db_factory).upsert(owner.id, "owned", content="instructions")
    directory = tmp_path / "skills" / skill.name
    directory.mkdir(parents=True)
    (directory / ".privateclaw-managed-skill.json").write_text(
        '{"version":1,"user_id":"%s","skill_id":"%s","name":"%s"}'
        % (owner.id, skill.id, skill.name),
        encoding="utf-8",
    )
    assert archive_owned_directory(tmp_path, owner.id, skill.id, skill.name) is None
    assert directory.exists()


async def test_only_managed_orphan_can_be_permanently_deleted(db_factory, stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("managed-orphan@test.local")
    managed = tmp_path / "skills" / "managed-orphan"
    managed.mkdir(parents=True)
    record_managed_directory(tmp_path, owner.id, "deleted-skill", managed.name)
    unmanaged = tmp_path / "skills" / "user-folder"
    unmanaged.mkdir()

    detected = find_orphans(tmp_path, owner.id, {})
    assert {item["name"]: item["managed"] for item in detected} == {
        "managed-orphan": True, "user-folder": False,
    }
    delete_managed_orphan(tmp_path, owner.id, managed.name, {})
    assert not managed.exists()
    with pytest.raises(ValueError, match="ownership-verified"):
        delete_managed_orphan(tmp_path, owner.id, unmanaged.name, {})
    assert unmanaged.exists()


async def test_stale_ownership_record_does_not_authorize_recreated_directory(stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("stale-owner@test.local")
    directory = tmp_path / "skills" / "recreated"
    directory.mkdir(parents=True)
    record_managed_directory(tmp_path, owner.id, "old-skill", directory.name)
    directory.rmdir()
    directory.mkdir()

    assert find_orphans(tmp_path, owner.id, {}) == [
        {"name": "recreated", "managed": False, "registered": False}
    ]
    with pytest.raises(ValueError, match="ownership-verified"):
        delete_managed_orphan(tmp_path, owner.id, directory.name, {})
    assert directory.is_dir()


async def test_orphans_are_detected_but_not_enabled(db_factory, stores, tmp_path):
    owner = await stores["users"].get_or_create_by_email("orphan@test.local")
    orphan = tmp_path / "skills" / "old-clone"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("old", encoding="utf-8")
    store = SkillStore(db_factory)
    assert find_orphans(tmp_path, owner.id, {}) == [
        {"name": "old-clone", "managed": False, "registered": False}
    ]
    assert await store.enabled_for_user(owner.id) == []


async def test_failed_install_rolls_back_partial_skill(db_factory, stores):
    owner = await stores["users"].get_or_create_by_email("rollback@test.local")
    store = SkillStore(db_factory)
    broken = parse_bundle(bundle())
    del broken["files"]
    with pytest.raises(KeyError):
        await install_bundle(store, owner.id, broken)
    assert await store.list_for_user(owner.id) == []


async def test_workspace_skill_references_warn_without_rewriting(db_factory, stores):
    owner = await stores["users"].get_or_create_by_email("reference-warning@test.local")
    warnings = await reference_warnings(
        SkillStore(db_factory), owner.id, "demo", "Read skills/demo/references/a.md and skills/missing/x.md"
    )
    assert len(warnings) == 2
    assert "bundle-relative" in warnings[0]
    assert "not registered" in warnings[1]


async def test_workspace_skill_reference_detection_supports_real_names(db_factory, stores):
    owner = await stores["users"].get_or_create_by_email("reference-names@test.local")
    warnings = await reference_warnings(
        SkillStore(db_factory),
        owner.id,
        "demo",
        "Read skills/Upper_Name/a.md, skills/ทักษะ/x.md, and `skills/Name With Space/ref.md`.",
    )
    assert len(warnings) == 3
    assert all("not registered" in warning for warning in warnings)


@pytest.mark.parametrize("manager", [ManageSkillTool, SbotManager])
async def test_plain_save_cannot_register_workspace_package(
    db_factory, stores, tmp_path, manager
):
    owner = await stores["users"].get_or_create_by_email(
        f"plain-package-{manager.__module__}@test.local"
    )
    store = SkillStore(db_factory)
    package = tmp_path / "skills" / "copied-package"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("package", encoding="utf-8")
    tool = manager(store, owner.id, workspace=tmp_path)
    result = await tool.execute(action="save", name="copied-package", content="instructions")
    assert "import_github" in result
    assert await store.get_by_name(owner.id, "copied-package") is None

    legacy = await store.upsert(owner.id, "legacy", content="old")
    result = await tool.execute(action="save", name=legacy.name, content="updated")
    assert "saved" in result


async def test_archive_failure_keeps_skill_registered(db_factory, stores, tmp_path, monkeypatch):
    owner = await stores["users"].get_or_create_by_email("archive-failure@test.local")
    store = SkillStore(db_factory)
    skill = await store.upsert(owner.id, "managed", content="instructions")
    directory = tmp_path / "skills" / skill.name
    directory.mkdir(parents=True)
    record_managed_directory(tmp_path, owner.id, skill.id, skill.name)

    def fail_replace(source, destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr("claw.skills.workspace.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        await delete_skill_with_workspace(store, tmp_path, owner.id, skill)
    assert await store.get_by_name(owner.id, skill.name) is not None
    assert directory.is_dir()


async def test_database_delete_failure_restores_staged_directory(
    db_factory, stores, tmp_path, monkeypatch
):
    owner = await stores["users"].get_or_create_by_email("delete-rollback@test.local")
    store = SkillStore(db_factory)
    skill = await store.upsert(owner.id, "managed", content="instructions")
    directory = tmp_path / "skills" / skill.name
    directory.mkdir(parents=True)
    record_managed_directory(tmp_path, owner.id, skill.id, skill.name)

    async def fail_delete(user_id, skill_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await delete_skill_with_workspace(store, tmp_path, owner.id, skill)
    assert directory.is_dir()


def test_skill_creator_documents_github_bundle_import():
    from claw.core.builtin_skills import get_builtin_skill
    from sbot.core.builtin_skills import get_builtin_skill as get_sbot_builtin_skill

    for lookup in (get_builtin_skill, get_sbot_builtin_skill):
        content = lookup("skill-creator").content
        assert 'action="import_github"' in content
        assert "Do not clone" in content


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
async def test_import_api_and_sharing_roundtrip(db_factory, bot_mode, tmp_path):
    from tests.conftest_app import build_api_app, client
    from tests.test_manage import _register, _bearer

    app = build_api_app(db_factory, workspaces_root=tmp_path)
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
        token, user = await _register(c, f"bundle-{bot_mode}@example.com")
        headers = _bearer(token)
        response = await c.post(
            "/api/skills/import", files={"file": ("skill.zip", bundle(), "application/zip")}, headers=headers
        )
        assert response.status_code == 200, response.text
        skill = response.json()
        assert skill["bundle"]["files"] == ["SKILL.md", "references/style.md"]
        async with db_factory() as db:
            stored = await db.scalar(select(Skill).where(Skill.id == skill["id"]))
            version = await db.get(SkillBundleVersion, stored.bundle_id)
            original = {
                "bundle_id": stored.bundle_id,
                "content": stored.content,
                "description": stored.description,
                "source": dict(version.source),
                "files": dict(version.files),
            }
        same_name_directory = tmp_path / user["id"] / "skills" / "demo"
        same_name_directory.mkdir(parents=True)
        marker = same_name_directory / "legacy.txt"
        marker.write_text("leave unchanged", encoding="utf-8")

        saved = await c.put(
            "/api/skills/demo",
            json={"name": "demo", "visibility": "public"},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["bundle"] == skill["bundle"]
        async with db_factory() as db:
            stored = await db.scalar(select(Skill).where(Skill.id == skill["id"]))
            version = await db.get(SkillBundleVersion, stored.bundle_id)
            assert stored.visibility == "public"
            assert stored.bundle_id == original["bundle_id"]
            assert stored.content == original["content"]
            assert stored.description == original["description"]
            assert version.source == original["source"]
            assert version.files == original["files"]
        assert marker.read_text(encoding="utf-8") == "leave unchanged"

        connector = await app.state.claw.connectors.upsert(
            user["id"], "bundle-connector", transport="http", url="https://example.test/mcp"
        )
        disabled = await c.put(
            "/api/skills/demo",
            json={**skill, "visibility": "public", "enabled": False, "connector_id": connector.id},
            headers=headers,
        )
        assert disabled.status_code == 200, disabled.text
        assert not disabled.json()["enabled"]
        assert disabled.json()["connector_id"] == connector.id
        enabled = await c.put(
            "/api/skills/demo",
            json={**skill, "visibility": "public", "enabled": True, "connector_id": None},
            headers=headers,
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["enabled"]
        assert enabled.json()["connector_id"] is None
        ignored_bundle_edit = await c.put(
            "/api/skills/demo",
            json={
                **skill,
                "visibility": "public",
                "bundle": {"source": "tampered", "version": "999", "files": {}},
            },
            headers=headers,
        )
        assert ignored_bundle_edit.status_code == 200, ignored_bundle_edit.text

        unshared = await c.put(
            "/api/skills/demo", json={**skill, "visibility": "private"}, headers=headers
        )
        assert unshared.status_code == 200, unshared.text
        async with db_factory() as db:
            stored = await db.get(Skill, skill["id"])
            version = await db.get(SkillBundleVersion, stored.bundle_id)
            assert stored.visibility == "private"
            assert stored.bundle_id == original["bundle_id"]
            assert stored.content == original["content"]
            assert stored.description == original["description"]
            assert version.source == original["source"]
            assert version.files == original["files"]
        assert (
            await c.put("/api/skills/demo", json={**skill, "content": "changed"}, headers=headers)
        ).status_code == 400
        assert (
            await c.put(
                "/api/skills/demo",
                json={**skill, "description": "changed"},
                headers=headers,
            )
        ).status_code == 400

        plain_directory = tmp_path / user["id"] / "skills" / "copied-package"
        plain_directory.mkdir()
        blocked = await c.put(
            "/api/skills/copied-package",
            json={"name": "copied-package", "content": "pretend package"},
            headers=headers,
        )
        assert blocked.status_code == 400
        assert "import_github" in blocked.text
        assert await app.state.claw.skills.get_by_name(user["id"], "copied-package") is None

        plain = await app.state.claw.skills.upsert(
            user["id"],
            "plain-existing",
            description="keep description",
            content="plain",
            enabled=False,
            connector_id=connector.id,
        )
        plain_same_name_directory = tmp_path / user["id"] / "skills" / plain.name
        plain_same_name_directory.mkdir()
        plain_shared = await c.put(
            f"/api/skills/{plain.name}",
            json={"name": plain.name, "visibility": "public"},
            headers=headers,
        )
        assert plain_shared.status_code == 200, plain_shared.text
        assert plain_shared.json()["visibility"] == "public"
        assert plain_shared.json()["description"] == "keep description"
        assert plain_shared.json()["content"] == "plain"
        assert not plain_shared.json()["enabled"]
        assert plain_shared.json()["connector_id"] == connector.id
        plain_content_change = await c.put(
            f"/api/skills/{plain.name}",
            json={"id": plain.id, "name": plain.name, "content": "changed"},
            headers=headers,
        )
        assert plain_content_change.status_code == 400
        invalid = await c.post(
            "/api/skills/import-github",
            json={"repository": "http://127.0.0.1", "commit": "a" * 40},
            headers=headers,
        )
        assert invalid.status_code == 400


@pytest.mark.parametrize("bot_mode", [False, True])
async def test_archive_permission_error_is_clear_and_keeps_registered_skill(
    db_factory, bot_mode, tmp_path, monkeypatch
):
    from tests.conftest_app import build_api_app, client
    from tests.test_manage import _bearer, _register

    app = build_api_app(db_factory, workspaces_root=tmp_path)
    if bot_mode:
        from claw.api.deps import current_user, get_state
        from sbot.api.deps import current_user as sbot_user
        from sbot.api.deps import get_state as sbot_state
        from sbot.api.manage import router
        from sbot.db.stores import SkillStore as SbotSkillStore

        app.router.routes = [
            route
            for route in app.router.routes
            if not getattr(route, "path", "").startswith("/api/skills")
        ]
        app.include_router(router)
        app.dependency_overrides[sbot_state] = get_state
        app.dependency_overrides[sbot_user] = current_user
        app.state.claw.skills = SbotSkillStore(db_factory)

    async with client(app) as c:
        token, user = await _register(c, f"archive-permission-{bot_mode}@example.com")
        skill = await app.state.claw.skills.upsert(
            user["id"], "permission-skill", content="instructions"
        )
        workspace = tmp_path / user["id"]
        directory = workspace / "skills" / skill.name
        directory.mkdir(parents=True)
        record_managed_directory(workspace, user["id"], skill.id, skill.name)

        def deny_archive(source, destination):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("claw.skills.workspace.os.replace", deny_archive)
        response = await c.delete(f"/api/skills/{skill.id}", headers=_bearer(token))
        assert response.status_code == 409
        assert "service account lacks permission" in response.json()["detail"]
        assert await app.state.claw.skills.get_by_name(user["id"], skill.name) is not None
        assert directory.is_dir()


@pytest.mark.parametrize("bot_mode", [False, True])
async def test_orphan_archive_permission_error_is_clear_and_keeps_state(
    db_factory, bot_mode, tmp_path, monkeypatch
):
    from tests.conftest_app import build_api_app, client
    from tests.test_manage import _bearer, _register

    app = build_api_app(db_factory, workspaces_root=tmp_path)
    if bot_mode:
        from claw.api.deps import current_user, get_state
        from sbot.api.deps import current_user as sbot_user
        from sbot.api.deps import get_state as sbot_state
        from sbot.api.manage import router
        from sbot.db.stores import SkillStore as SbotSkillStore

        app.router.routes = [
            route
            for route in app.router.routes
            if not getattr(route, "path", "").startswith("/api/skills")
        ]
        app.include_router(router)
        app.dependency_overrides[sbot_state] = get_state
        app.dependency_overrides[sbot_user] = current_user
        app.state.claw.skills = SbotSkillStore(db_factory)

    async with client(app) as c:
        token, user = await _register(c, f"orphan-permission-{bot_mode}@example.com")
        skill = await app.state.claw.skills.upsert(
            user["id"], "same-name-orphan", content="registered instructions"
        )
        directory = tmp_path / user["id"] / "skills" / skill.name
        directory.mkdir(parents=True)
        marker = directory / "legacy.txt"
        marker.write_text("keep", encoding="utf-8")

        def deny_archive(source, destination):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("claw.skills.workspace.os.replace", deny_archive)
        response = await c.post(
            "/api/skills-workspace/orphans/archive",
            json={"name": skill.name},
            headers=_bearer(token),
        )
        assert response.status_code == 409
        assert "service account lacks permission" in response.json()["detail"]
        stored = await app.state.claw.skills.get_by_name(user["id"], skill.name)
        assert stored is not None and stored.id == skill.id
        assert marker.read_text(encoding="utf-8") == "keep"


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
