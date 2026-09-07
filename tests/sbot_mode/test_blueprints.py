import pytest

from sbot.api.blueprints import _safe_name
from sbot.db.stores import BlueprintStore, GroupStore
from sbot.tools.blueprint import SaveBlueprintTool


@pytest.mark.parametrize("filename", [
    "แบบฟอร์มเบิกค่ารักษาพยาบาล.xlsx",
    "แบบฟอร์มเสนอราคา.docx",
    "แบบฟอร์มโครงการ.pptx",
])
def test_safe_name_preserves_supported_extension_for_non_ascii_filename(filename):
    safe = _safe_name(filename)
    assert safe.startswith(filename.rsplit(".", 1)[0])
    assert safe.endswith(filename.rsplit(".", 1)[-1])
    assert safe.rsplit(".", 1)[-1].lower() in {"docx", "xlsx", "pptx"}


@pytest.mark.asyncio
async def test_blueprint_visibility_and_versions(stores):
    users = stores["users"]
    groups = GroupStore(users.factory)
    team = await groups.create("Finance")
    owner = await users.create("owner@example.com", group_id=team.id)
    teammate = await users.create("team@example.com", group_id=team.id)
    outsider = await users.create("outside@example.com")
    store = BlueprintStore(users.factory)

    group_bp = await store.create(
        blueprint_id="bp-group",
        owner_id=owner.id,
        name="Daily quote",
        description="",
        visibility="group",
        filename="quote.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=12,
        storage_path="bp-group/v1/quote.docx",
    )
    await store.create(
        blueprint_id="bp-public",
        owner_id=owner.id,
        name="Proposal",
        description="",
        visibility="public",
        filename="proposal.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=20,
        storage_path="bp-public/v1/proposal.pptx",
    )

    assert {row["id"] for row in await store.list_accessible(teammate.id)} == {"bp-group", "bp-public"}
    assert {row["id"] for row in await store.list_accessible(outsider.id)} == {"bp-public"}

    version = await store.add_version(
        blueprint_id=group_bp.id,
        created_by=owner.id,
        filename="quote-v2.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=15,
        storage_path_for_version=lambda number: f"bp-group/v{number}/quote-v2.docx",
    )
    assert version is not None and version.version == 2
    assert (await store.get(group_bp.id)).current_version == 2

    await store.activate_version(group_bp.id, 1)
    assert (await store.get(group_bp.id)).current_version == 1
    assert [row.version for row in await store.list_versions(group_bp.id)] == [2, 1]


@pytest.mark.asyncio
async def test_private_blueprint_is_owner_only(stores):
    users = stores["users"]
    owner = await users.create("private-owner@example.com")
    viewer = await users.create("private-viewer@example.com")
    store = BlueprintStore(users.factory)
    await store.create(
        blueprint_id="bp-private",
        owner_id=owner.id,
        name="Approval form",
        description="",
        visibility="private",
        filename="approval.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=8,
        storage_path="bp-private/v1/approval.xlsx",
    )

    assert [row["id"] for row in await store.list_accessible(owner.id)] == ["bp-private"]
    assert await store.list_accessible(viewer.id) == []


@pytest.mark.asyncio
async def test_save_blueprint_tool_copies_workspace_file_without_linking_source(stores, tmp_path):
    user = await stores["users"].create("tool-owner@example.com")
    store = BlueprintStore(stores["users"].factory)
    workspace = tmp_path / "workspace"
    root = tmp_path / "blueprints"
    workspace.mkdir()
    source = workspace / "quote.docx"
    source.write_bytes(b"template-v1")

    result = await SaveBlueprintTool(store, root, workspace, user.id).execute(
        path="quote.docx", name="Daily quote"
    )
    assert result.startswith("Saved Blueprint")
    row = (await store.list_accessible(user.id))[0]
    version = await store.get_version(row["id"], 1)
    assert version is not None

    source.write_bytes(b"changed-working-copy")
    assert (root / version.storage_path).read_bytes() == b"template-v1"
