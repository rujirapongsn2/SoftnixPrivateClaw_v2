"""Skill tools: manage_skill persists to the store; read_skill + built-ins."""

from claw.core.builtin_skills import builtin_skills, get_builtin_skill
from claw.db.stores import SkillStore
from claw.tools.skills import ManageSkillTool, ReadSkillTool


async def test_manage_skill_save_persists_and_lists(db_factory, stores):
    store = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("sk@x.y")
    tool = ManageSkillTool(store, user.id)

    out = await tool.execute(
        action="save",
        name="stock-analysis",
        description="Analyse a stock",
        content="1. fetch prices\n2. plot",
    )
    assert "saved" in out.lower()

    # It is now a real row the Settings list endpoint would return.
    rows = await store.list_for_user(user.id)
    assert [r.name for r in rows] == ["stock-analysis"]
    assert rows[0].content.startswith("1. fetch prices")

    listing = await tool.execute(action="list")
    assert "stock-analysis" in listing


async def test_manage_skill_validates_and_deletes(db_factory, stores):
    store = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("sk2@x.y")
    tool = ManageSkillTool(store, user.id)

    assert (await tool.execute(action="save", name="x")).startswith("Error: save requires 'content'")
    assert (await tool.execute(action="save", content="c")).startswith("Error: save requires a 'name'")
    assert (await tool.execute(action="delete", name="nope")).startswith("Error: skill 'nope' not found")

    await tool.execute(action="save", name="temp", content="do a thing")
    out = await tool.execute(action="delete", name="temp")
    assert out == "Skill 'temp' deleted."
    assert await store.list_for_user(user.id) == []


async def test_manage_skill_cannot_shadow_builtin(db_factory, stores):
    store = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("sk3@x.y")
    tool = ManageSkillTool(store, user.id)
    out = await tool.execute(action="save", name="skill-creator", content="hijack")
    assert "reserved" in out.lower()
    assert await store.list_for_user(user.id) == []


async def test_read_skill_falls_back_to_builtin(db_factory, stores):
    store = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("sk4@x.y")
    tool = ReadSkillTool(store, user.id)
    out = await tool.execute(name="skill-creator")
    assert out.startswith("# Skill: skill-creator")
    assert "manage_skill" in out


def test_builtin_catalog():
    names = {s.name for s in builtin_skills()}
    assert "skill-creator" in names
    assert get_builtin_skill("skill-creator") is not None
    assert get_builtin_skill("nope") is None
