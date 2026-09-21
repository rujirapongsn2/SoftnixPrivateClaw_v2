"""What a Team Lead actually hands a specialist when it creates one.

Nothing infers tools or skills from a charter — the leader is an LLM naming
them, and everything here is about the gap between what it named and what the
new bot can do. Two things used to fall into that gap: a bot kept its document
readers when its leader delegated to it and lost them the moment the user
opened its own chat, and a skill name that matched nothing was accepted and
silently removed every skill the bot had.
"""

import json
import re
from pathlib import Path

from sbot.core.specialist import DELEGATABLE_TOOLS, SPECIALIST_ALWAYS_TOOLS
from sbot.db.stores import SkillStore
from sbot.tools.cos import CreateBotsTool, CreateBotTool
from sbot.tools.registry import ALWAYS_AVAILABLE_TOOLS, INTRINSIC_TOOLS


def test_both_paths_protect_the_same_tools():
    """The delegate path and direct chat must agree on what an allowlist cannot
    take away. While they were two hand-written lists they disagreed, and the
    same bot answered differently depending on who asked it."""
    assert SPECIALIST_ALWAYS_TOOLS <= ALWAYS_AVAILABLE_TOOLS


def test_a_grantable_tool_is_never_also_free():
    """Every name a leader (or the Settings checkboxes) can grant must actually
    be withheld until granted — otherwise the choice is decoration."""
    assert not DELEGATABLE_TOOLS & ALWAYS_AVAILABLE_TOOLS


def test_the_editor_offers_exactly_the_grantable_tools():
    source = Path(__file__).parents[2] / "web/src/sbot/BotEditor.tsx"
    block = re.search(r"const BOT_TOOLS = \[(.*?)\] as const;", source.read_text(), re.S)
    assert block, "BOT_TOOLS moved; this guard needs updating"
    offered = set(re.findall(r'\["([a-z_]+)",', block.group(1)))
    assert offered == set(DELEGATABLE_TOOLS)


async def test_a_restricted_bot_keeps_its_document_readers(stores, db_factory, tmp_path):
    """The bug this file exists for: `read_pdf` is not a capability a narrow
    allowlist should remove, and the specialist path never removed it."""
    from sbot.tools.registry import ToolRegistry

    registry = ToolRegistry()
    from sbot.tools.documents import build_document_tools

    for tool in build_document_tools(tmp_path):
        registry.register(tool)
    registry.restrict_to(["read_file"])
    for name in ("read_pdf", "read_excel", "read_csv", "read_docx"):
        assert registry.has(name), name


async def test_an_empty_allowlist_still_leaves_the_bot_its_own_machinery():
    from sbot.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.restrict_to([])
    assert registry._is_allowed("recall_memory")
    assert registry._is_allowed("search_knowledge")
    assert not registry._is_allowed("exec")


def test_the_schema_names_the_tools_it_will_accept():
    """A leader that has to guess a tool name gets the whole call rejected, so
    the legal names and the permissive default belong in the description."""
    described = CreateBotTool.parameters["properties"]["tool_allowlist"]["description"]
    for name in DELEGATABLE_TOOLS:
        assert name in described
    assert "OMITTING THIS GRANTS EVERY CAPABILITY" in described
    member = CreateBotsTool.parameters["properties"]["bots"]["items"]["properties"]
    assert member["tool_allowlist"]["description"] == described


async def _create_tool(stores, db_factory, email: str, cls=CreateBotTool):
    user = await stores["users"].get_or_create_by_email(email)
    skills = SkillStore(db_factory)
    tool = cls(stores["bots"], user.id, creator_bot_id="cos", skills=skills)
    return user, skills, tool


async def test_an_invented_skill_name_is_refused_with_the_real_ones(stores, db_factory):
    user, skills, tool = await _create_tool(stores, db_factory, "invented@sbot.ai")
    await skills.upsert(user.id, "seo-audit", description="d", content="c", enabled=True)

    result = await tool.execute(
        name="SEO", role_title="SEO Expert", charter="c", skill_ids=["SEO Research"]
    )

    assert result.startswith("Error:")
    assert "SEO Research" in result and "seo-audit" in result
    assert await stores["bots"].get_by_name(user.id, "SEO") is None


async def test_a_named_skill_is_stored_as_its_id(stores, db_factory):
    """Stored by id so renaming the skill later does not detach it from the
    bots that were built around it."""
    user, skills, tool = await _create_tool(stores, db_factory, "named@sbot.ai")
    skill = await skills.upsert(user.id, "seo-audit", description="d", content="c", enabled=True)

    await tool.execute(
        name="SEO", role_title="SEO Expert", charter="c", skill_ids=["SEO-Audit"]
    )

    bot = await stores["bots"].get_by_name(user.id, "SEO")
    assert bot.skill_ids == [skill.id]


async def test_a_builtin_skill_resolves_too(stores, db_factory):
    from sbot.core.builtin_skills import builtin_skills

    user, _, tool = await _create_tool(stores, db_factory, "builtin@sbot.ai")
    builtin = builtin_skills()[0]

    await tool.execute(
        name="Writer", role_title="Writer", charter="c", skill_ids=[builtin.name]
    )

    bot = await stores["bots"].get_by_name(user.id, "Writer")
    assert bot.skill_ids == [builtin.id]


async def test_no_skill_list_still_means_every_skill(stores, db_factory):
    user, _, tool = await _create_tool(stores, db_factory, "allskills@sbot.ai")

    await tool.execute(name="Generalist", role_title="Generalist", charter="c")

    bot = await stores["bots"].get_by_name(user.id, "Generalist")
    assert bot.skill_ids is None


async def test_one_bad_skill_name_creates_no_bots_at_all(stores, db_factory):
    user, skills, tool = await _create_tool(stores, db_factory, "batch@sbot.ai", CreateBotsTool)
    await skills.upsert(user.id, "seo-audit", description="d", content="c", enabled=True)

    result = await tool.execute(bots=[
        {"name": "A", "role_title": "r", "charter": "c", "skill_ids": ["seo-audit"]},
        {"name": "B", "role_title": "r", "charter": "c", "skill_ids": ["nope"]},
    ])

    assert result.startswith("Error:") and "'B'" in result
    assert await stores["bots"].get_by_name(user.id, "A") is None


async def test_an_unknown_tool_is_still_refused(stores, db_factory):
    user, _, tool = await _create_tool(stores, db_factory, "badtool@sbot.ai")

    result = await tool.execute(
        name="Nope", role_title="r", charter="c", tool_allowlist=["search_web"]
    )

    assert result.startswith("Error:") and "search_web" in result
    assert await stores["bots"].get_by_name(user.id, "Nope") is None


async def test_naming_a_free_tool_is_redundant_rather_than_fatal(stores, db_factory):
    """The tool description spells out `read_pdf` and friends by name; refusing
    the leader that copied them from it made the field unusable as documented."""
    user, _, tool = await _create_tool(stores, db_factory, "freetool@sbot.ai")

    result = await tool.execute(
        name="Reader", role_title="r", charter="c",
        tool_allowlist=["read_file", "read_pdf", "search_knowledge"],
    )

    assert not result.startswith("Error:"), result
    bot = await stores["bots"].get_by_name(user.id, "Reader")
    assert "read_pdf" in bot.tool_allowlist


async def test_a_list_of_blanks_is_not_read_as_no_skills(stores, db_factory):
    """`[""]` used to strip down to `[]`, which means the opposite of omitting
    the field: a specialist that can see no skill at all."""
    user, _, tool = await _create_tool(stores, db_factory, "blank@sbot.ai")

    result = await tool.execute(
        name="Blank", role_title="r", charter="c", skill_ids=["", "  "]
    )

    assert result.startswith("Error:")
    assert await stores["bots"].get_by_name(user.id, "Blank") is None


async def test_an_empty_skill_list_still_means_no_skills(stores, db_factory):
    user, _, tool = await _create_tool(stores, db_factory, "noskills@sbot.ai")

    await tool.execute(name="Quiet", role_title="r", charter="c", skill_ids=[])

    bot = await stores["bots"].get_by_name(user.id, "Quiet")
    assert bot.skill_ids == []


async def test_a_switched_off_skill_says_so(stores, db_factory):
    """It is sitting in the user's Skills panel, so "no skill named it" reads as
    the tool being broken rather than as something the user can fix."""
    user, skills, tool = await _create_tool(stores, db_factory, "disabled@sbot.ai")
    await skills.upsert(user.id, "seo-audit", description="d", content="c", enabled=False)

    result = await tool.execute(
        name="SEO", role_title="r", charter="c", skill_ids=["seo-audit"]
    )

    assert result.startswith("Error:") and "switched off" in result
    assert await stores["bots"].get_by_name(user.id, "SEO") is None


async def test_the_owners_own_skill_wins_a_case_only_collision(stores, db_factory):
    """`ix_skills_user_name` does not fold case, so `pdf` and `PDF` coexist and
    a last-wins map silently attached the bot to whichever sorted later."""
    from sbot.core.builtin_skills import builtin_skills

    user, skills, tool = await _create_tool(stores, db_factory, "collide@sbot.ai")
    builtin = builtin_skills()[0]
    mine = await skills.upsert(
        user.id, builtin.name.upper(), description="d", content="c", enabled=True
    )

    await tool.execute(
        name="Mine", role_title="r", charter="c", skill_ids=[builtin.name]
    )

    bot = await stores["bots"].get_by_name(user.id, "Mine")
    assert bot.skill_ids == [mine.id]


def test_a_delegated_specialist_can_actually_search_knowledge(tmp_path):
    """`search_knowledge` is always-available, which said every bot has it while
    the specialist path never built one: the same bot answered from the user's
    documents in its own chat and reported the tool missing when delegated to."""
    from sbot.core.specialist import SpecialistRunner

    runner = SpecialistRunner(None, None, tmp_path, owner_id="alice", knowledge=object())
    assert runner.build_tools(["read_file"]).has("search_knowledge")


def test_render_diagram_does_not_depend_on_the_skill_store(tmp_path):
    """It is advertised to the leader as intrinsic, so a caller that happens not
    to pass skills must not silently lose it."""
    from sbot.core.specialist import SpecialistRunner

    runner = SpecialistRunner(None, None, tmp_path, owner_id="alice")
    assert runner.build_tools(None).has("render_diagram")


def test_intrinsic_tools_are_not_orchestration():
    """`INTRINSIC_TOOLS` is the set both paths share; keeping the Chief of Staff
    tools out of it is what stops a specialist being handed `delegate`."""
    assert "delegate" not in INTRINSIC_TOOLS
    assert "exec" not in INTRINSIC_TOOLS
    assert json.dumps(sorted(INTRINSIC_TOOLS))
