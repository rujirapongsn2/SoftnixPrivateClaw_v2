"""Sbot's skill reader pages content before the loop's tool-result cap."""

from sbot.db.stores import SkillStore
from sbot.tools.skills import ReadSkillTool


async def test_sbot_read_skill_pages_and_selects_section(db_factory, stores):
    store = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("sbot-skill-page@x.y")
    await store.upsert(
        user.id,
        "large",
        description="",
        content="```sh\n# First\nnot-a-section\n```\n# First\n" + "A" * 9_000 + "\n# Second\n" + "B" * 9_000,
    )
    tool = ReadSkillTool(store, user.id)

    first = await tool.execute(name="large", section="First", limit=8_000)
    rest = await tool.execute(name="large", section="First", offset=8_000, limit=8_000)

    assert first.count("A") == 7_992  # the section heading occupies 8 characters
    assert "offset=8000" in first
    assert rest.count("A") == 1_008
    assert "B" not in first + rest
    assert "not-a-section" not in first + rest
