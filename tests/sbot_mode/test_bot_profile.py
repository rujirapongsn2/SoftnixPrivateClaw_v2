"""A bot's profile is its behaviour contract (PRD §3.1 / §4.5).

charter, tool_allowlist and skill_ids only mean something if the turn that runs
the bot actually reads them. These cover the direct-chat path specifically:
the delegate/mission path already went through SpecialistRunner, so it was the
one place a bot's boundaries were enforced — a bot the user opened in chat had
every tool in the process.
"""

from types import SimpleNamespace

import pytest

from sbot.api.manage import UpdateBotBody, update_bot
from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.context import ContextAssembler, PromptSection
from sbot.core.memory import MemoryService
from sbot.core.runtime import AgentRuntime
from sbot.core.turn_context import current_session_id
from sbot.db.stores import SkillStore
from sbot.providers.base import ChatResult, ToolCall
from sbot.tools.plan import PlanTool
from tests.sbot_mode.conftest import FakeProvider, text_turn


class _DisconnectedConnectors:
    """A ConnectorManager whose connectors are all configured but none connected.

    resolve_tool_names answers None in that case, which is the ordinary state of
    a skill whose connector the user has merely disabled.
    """

    class _Store:
        async def list_for_user(self, user_id):
            return []

        async def list_for_global(self):
            return []

        async def enabled_accessible(self, user_id):
            return []

    def __init__(self):
        self.store = self._Store()

    async def sync_tools(self, user_id, registry):
        return None

    async def resolve_tool_names(self, user_id, connector_id, **_):
        return None

    def status_summary(self, user_id):
        return {}


def make_runtime(stores, provider, tmp_path, skills=None, knowledge=None, connectors=None) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings,
        provider=provider,
        bus=EventBus(),
        users=stores["users"],
        bots=stores["bots"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=memory,
        audit=stores["audit"],
        skills=skills,
        knowledge=knowledge,
        connectors=connectors,
    )


async def _bot_session(stores, email: str, **bot_fields):
    user = await stores["users"].get_or_create_by_email(email)
    bot = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", **bot_fields
    )
    session = await stores["sessions"].create(user.id, title="chat", bot_id=bot.id, kind="direct")
    return user, bot, session


def system_text(call: list[dict]) -> str:
    return next(m["content"] for m in call if m["role"] == "system")


@pytest.mark.asyncio
async def test_a_bots_allowlist_applies_when_the_user_chats_with_it_directly(stores, tmp_path):
    user, _, session = await _bot_session(
        stores, "allow@sbot.ai", charter="หาข้อมูลเท่านั้น", tool_allowlist=["web_search"]
    )
    provider = FakeProvider([text_turn("ค้นแล้วครับ")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ช่วยหาข้อมูล")

    offered = set(provider.offered_tools[0])
    assert "web_search" in offered
    # The point of the allowlist: a research bot cannot run shell commands,
    # write files, or open a browser just because it was opened in chat.
    assert offered.isdisjoint({"exec", "write_file", "edit_file", "browser", "spawn"})
    # ...but the agent's own machinery is not a capability grant. A bot that
    # cannot remember anything would break per-bot memory entirely.
    assert {"remember", "update_plan"} <= offered


@pytest.mark.asyncio
async def test_a_tool_outside_the_allowlist_is_refused_even_when_the_model_calls_it(
    stores, tmp_path
):
    """Filtering the definitions is not enough on its own: a model that saw
    `exec` in an earlier turn (or invented it) can still emit the call."""
    user, _, session = await _bot_session(
        stores, "refuse@sbot.ai", tool_allowlist=["web_search"]
    )
    provider = FakeProvider([
        [ChatResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="exec", arguments={"command": "id"})],
        )],
        text_turn("ทำไม่ได้ครับ ผมไม่มีสิทธิ์รันคำสั่ง"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "รัน id ให้หน่อย")

    tool_results = [m for m in provider.calls[1] if m.get("role") == "tool"]
    assert len(tool_results) == 1
    assert "not available to this bot" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_a_bot_with_no_allowlist_is_unrestricted(stores, tmp_path):
    """Null allowlist is the default for every bot Chief of Staff creates
    without one, so it must keep meaning "everything" — not "nothing"."""
    user, _, session = await _bot_session(stores, "open@sbot.ai", charter="ทำได้ทุกอย่าง")
    provider = FakeProvider([text_turn("ok")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "hi")

    assert {"exec", "write_file", "web_search"} <= set(provider.offered_tools[0])


@pytest.mark.asyncio
async def test_widening_an_allowlist_takes_effect_on_the_next_message(stores, tmp_path):
    """Agents are cached per (user, bot), so a boundary applied once at
    construction would keep enforcing an allowlist the user has since edited —
    for as long as the cache happened to hold that agent."""
    user, bot, session = await _bot_session(
        stores, "widen@sbot.ai", tool_allowlist=["web_search"]
    )
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "หนึ่ง")
    await stores["bots"].update(bot.id, user.id, tool_allowlist=None)
    await runtime.handle_message(user.id, session.id, "สอง")

    assert "exec" not in provider.offered_tools[0]
    assert "exec" in provider.offered_tools[1]


def test_a_tool_registered_after_the_restriction_is_still_covered():
    """MCP connector tools are synced into the registry mid-turn, after the
    allowlist has been applied. Enforcing at read time rather than at
    registration is what stops a connector widening a bot's boundary."""
    from sbot.tools.registry import ToolRegistry
    from sbot.tools.web import WebSearchTool

    registry = ToolRegistry()
    registry.restrict_to(["web_search"])
    registry.register(WebSearchTool())

    class _LateTool(WebSearchTool):
        name = "mcp_gmail_send"

    registry.register(_LateTool())

    assert registry.tool_names == ["web_search"]
    assert registry.has("mcp_gmail_send") is False


@pytest.mark.asyncio
async def test_skills_are_scoped_to_the_bot_they_were_assigned_to(stores, db_factory, tmp_path):
    skills = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("skills@sbot.ai")
    market = await skills.upsert(user.id, "market-research", description="หาข้อมูลตลาด")
    await skills.upsert(user.id, "payroll", description="คำนวณเงินเดือน")

    researcher = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", skill_ids=[market.id]
    )
    generalist = await stores["bots"].create(
        owner_id=user.id, name="Generalist", role_title="Assistant"
    )
    scoped = await stores["sessions"].create(user.id, bot_id=researcher.id, kind="direct")
    unscoped = await stores["sessions"].create(user.id, bot_id=generalist.id, kind="direct")

    provider = FakeProvider([text_turn("a"), text_turn("b")])
    runtime = make_runtime(stores, provider, tmp_path, skills=skills)

    await runtime.handle_message(user.id, scoped.id, "งานวิจัย")
    await runtime.handle_message(user.id, unscoped.id, "อะไรก็ได้")

    curated = system_text(provider.calls[0])
    assert "market-research" in curated
    assert "payroll" not in curated
    # Curation is exhaustive, not additive: the built-ins are skills too, so a
    # bot given one skill is a bot with one skill. Otherwise "assigned skills"
    # would silently mean "assigned skills plus eight document ones".
    assert "- docx:" not in curated

    # A bot with no skill list still sees everything the owner has enabled.
    everything = system_text(provider.calls[1])
    assert "market-research" in everything and "payroll" in everything


class _FakeKnowledge:
    """Stands in for KnowledgeStore: only list_accessible is reached here, and
    registering SearchKnowledgeTool needs nothing from it."""

    async def list_accessible(self, user_id: str) -> list[dict]:
        return [{"name": "handbook", "description": "คู่มือพนักงาน", "docs": 3}]


@pytest.mark.asyncio
async def test_the_prompt_does_not_advertise_a_tool_the_allowlist_removed(stores, tmp_path):
    """A prompt that names an unavailable tool is worse than one that stays
    quiet: the model follows the instruction, the registry refuses the call,
    and the user gets an apology instead of an answer."""
    user, _, session = await _bot_session(
        stores, "kb@sbot.ai", tool_allowlist=["web_search"]
    )
    provider = FakeProvider([text_turn("ok")])
    runtime = make_runtime(stores, provider, tmp_path, knowledge=_FakeKnowledge())

    await runtime.handle_message(user.id, session.id, "อ่านคู่มือให้หน่อย")

    prompt = system_text(provider.calls[0])
    assert "search_knowledge" not in prompt
    assert "handbook" not in prompt
    assert "search_knowledge" not in provider.offered_tools[0]


@pytest.mark.asyncio
async def test_an_unrestricted_bot_still_hears_about_the_knowledge_bases(stores, tmp_path):
    user, _, session = await _bot_session(stores, "kb-open@sbot.ai")
    provider = FakeProvider([text_turn("ok")])
    runtime = make_runtime(stores, provider, tmp_path, knowledge=_FakeKnowledge())

    await runtime.handle_message(user.id, session.id, "อ่านคู่มือให้หน่อย")

    prompt = system_text(provider.calls[0])
    assert "handbook" in prompt
    assert "search_knowledge" in prompt


@pytest.mark.asyncio
async def test_a_deleted_bot_stops_answering_in_its_old_session(stores, tmp_path):
    """Deletion is a soft delete and sessions keep their bot_id forever, so a
    lookup that ignored is_archived let a bot the user had deleted keep running
    turns — with its charter, its allowlist and its private memory — while the
    bot list and the API both reported it gone."""
    user, bot, session = await _bot_session(
        stores, "archived@sbot.ai", charter="ผมคือบอทที่ถูกลบแล้ว", tool_allowlist=["web_search"]
    )
    await stores["bots"].archive(bot.id, user.id)
    provider = FakeProvider([text_turn("ok")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ยังอยู่ไหม")

    prompt = system_text(provider.calls[0])
    assert "ผมคือบอทที่ถูกลบแล้ว" not in prompt
    # Fell back to the owner's own Chief of Staff, which carries no allowlist.
    assert "exec" in provider.offered_tools[0]
    assert await stores["bots"].get(bot.id, user.id) is None


@pytest.mark.asyncio
async def test_the_session_plan_reaches_the_model(stores, tmp_path):
    """render_plan was being built and then dropped on the floor: the runtime
    passed plan_context into a prompt builder that never appended it, so the
    pinned plan the update_plan tool maintains never actually reached a turn."""
    user, _, session = await _bot_session(stores, "plan@sbot.ai")
    await stores["sessions"].set_plan(
        session.id, "เปิดตัวเว็บใหม่", [{"step": "ทำ landing page", "status": "in_progress"}]
    )
    provider = FakeProvider([text_turn("รับทราบ")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ต่อจากเดิม")

    prompt = system_text(provider.calls[0])
    assert "เปิดตัวเว็บใหม่" in prompt
    assert "ทำ landing page" in prompt


@pytest.mark.asyncio
async def test_the_plan_survives_a_window_too_small_for_the_rest(stores, tmp_path):
    """The plan section was droppable while three separate places — the tool's
    description, its own rendered text, and the core prompt — told the model it
    is pinned. It is the one section nothing can fetch back: `update_plan` only
    writes, and it replaces the stored plan, so a model that lost its plan can
    only invent a new one over the real one.
    """
    user, _, session = await _bot_session(stores, "pinplan@sbot.ai", charter="ทำงานยาว")
    await stores["sessions"].set_plan(
        session.id, "ย้ายระบบขึ้น cloud", [{"step": "สำรวจ dependency", "status": "in_progress"}]
    )
    # A memory document big enough that the window cannot hold both.
    await stores["memories"].set_core(user.id, "## Facts\n" + "m " * 4_000)
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.assembler = ContextAssembler(
        token_counter=provider.count_tokens, max_context_tokens=1_500
    )

    await runtime.handle_message(user.id, session.id, "ต่อเลย")

    prompt = system_text(provider.calls[0])
    assert "ย้ายระบบขึ้น cloud" in prompt
    assert "สำรวจ dependency" in prompt


@pytest.mark.asyncio
async def test_a_plan_cannot_grow_the_pinned_prompt_without_limit(stores):
    """Pinning means never trimmed, so the model writing the plan must not get
    to decide how much of its own window the plan occupies."""
    user = await stores["users"].get_or_create_by_email("bigplan@sbot.ai")
    session = await stores["sessions"].create(user.id, title="chat", kind="direct")
    tool = PlanTool(stores["sessions"])
    token = current_session_id.set(session.id)
    try:
        result = await tool.execute(
            goal="g " * 5_000,
            steps=[{"step": f"{i} " + "s " * 1_000, "status": "pending"} for i in range(200)],
        )
    finally:
        current_session_id.reset(token)

    assert "dropped" in result
    stored = (await stores["sessions"].get(session.id)).plan
    assert len(stored["goal"]) <= 500
    assert len(stored["steps"]) <= 40
    assert all(len(s["step"]) <= 300 for s in stored["steps"])


# ----------------------------------------------------------- prompt trimming


def _sections() -> list[PromptSection]:
    return [
        PromptSection("core", "# Core\n" + "identity " * 200, pinned=True),
        PromptSection("charter", "# Charter\nYOU-ARE-THE-ANALYST", pinned=True),
        PromptSection("memory", "# Memory\nMEMORY-BLOCK " + "m " * 200),
        PromptSection("plan", "# Plan\nPLAN-BLOCK " + "p " * 200),
        PromptSection("skills", "# Skills\nSKILLS-BLOCK " + "s " * 200),
        PromptSection("knowledge", "# Knowledge\nKNOWLEDGE-BLOCK " + "k " * 200),
    ]


def test_a_tight_window_drops_the_lowest_priority_sections_first():
    """PRD §4.5 orders the prompt so what goes first is what can be fetched
    again: a dropped knowledge listing costs a `search_knowledge` call, a
    dropped charter costs the bot its identity for the whole turn."""
    assembler = ContextAssembler(max_context_tokens=400)

    prompt = assembler.fit_system_prompt(_sections())

    assert "YOU-ARE-THE-ANALYST" in prompt
    assert "KNOWLEDGE-BLOCK" not in prompt
    assert "SKILLS-BLOCK" not in prompt
    # Dropped silently, the model would answer as though the user has no
    # skills or documents at all.
    assert "knowledge" in prompt and "skills" in prompt


def test_the_charter_survives_a_window_too_small_for_anything():
    assembler = ContextAssembler(max_context_tokens=1)

    prompt = assembler.fit_system_prompt(_sections())

    assert "YOU-ARE-THE-ANALYST" in prompt
    assert "MEMORY-BLOCK" not in prompt


def test_a_fitting_prompt_keeps_every_section_in_priority_order():
    assembler = ContextAssembler(max_context_tokens=100_000)

    prompt = assembler.fit_system_prompt(_sections())

    assert "Omitted this turn" not in prompt
    assert prompt.index("YOU-ARE-THE-ANALYST") < prompt.index("MEMORY-BLOCK")
    assert prompt.index("MEMORY-BLOCK") < prompt.index("PLAN-BLOCK")
    assert prompt.index("PLAN-BLOCK") < prompt.index("SKILLS-BLOCK")


def test_a_long_pasted_message_does_not_cost_the_bot_its_memory():
    """The history reserve used to be a slice of the whole window taken on top
    of the current message, which double-counted: a paste that still left room
    for everything drove the budget below zero, and the turn where the user
    pastes a document and says "use my usual preferences" was exactly the turn
    the preferences were dropped.

    The window and the paste are sized to sit in the band where only that
    arithmetic decides the outcome — above _PROMPT_FLOOR, which would otherwise
    keep the sections on its own and make this pass either way, and below the
    point where the prompt genuinely does not fit.
    """
    sections = [
        PromptSection("core", "# Core\n" + "identity " * 200, pinned=True),
        PromptSection("charter", "# Charter\nYOU-ARE-THE-ANALYST", pinned=True),
        PromptSection("memory", "# Memory\nMEMORY-BLOCK " + "m " * 3_000),
        PromptSection("plan", "# Plan\nPLAN-BLOCK " + "p " * 200),
        PromptSection("skills", "# Skills\nSKILLS-BLOCK " + "s " * 200),
        PromptSection("knowledge", "# Knowledge\nKNOWLEDGE-BLOCK " + "k " * 200),
    ]
    assembler = ContextAssembler(max_context_tokens=3_800)
    paste = {"role": "user", "content": "เอกสาร " * 340}
    # Comfortably inside the window on its own — the whole prompt fits too.
    assert assembler.count_tokens([paste]) < 700

    messages = assembler.assemble(sections, [], paste)

    assert "MEMORY-BLOCK" in messages[0]["content"]
    assert "SKILLS-BLOCK" in messages[0]["content"]
    assert "Omitted this turn" not in messages[0]["content"]


def test_a_miscounted_attachment_does_not_strip_the_prompt():
    """`reserved` comes off the window, so a message that measures larger than
    the window takes the prompt budget to zero and drops everything droppable.

    That measurement is the least trustworthy input here: the fallback counter
    is `len(json.dumps(...)) // 4`, so an inlined base64 image reads as ~2M
    tokens where the real cost is under a hundred. Without a floor, attaching
    one photo costs the bot its memory and its skills for that turn.
    """
    assembler = ContextAssembler(max_context_tokens=100_000)

    prompt = assembler.fit_system_prompt(_sections(), reserved=2_000_000)

    assert "MEMORY-BLOCK" in prompt
    assert "SKILLS-BLOCK" in prompt
    assert "Omitted this turn" not in prompt


def test_the_omitted_note_is_paid_for_out_of_the_budget_it_reports():
    """The note is appended after trimming, so its own cost has to be charged
    while trimming — otherwise fitting the prompt to the budget and then
    reporting what was dropped puts it back over."""
    assembler = ContextAssembler(max_context_tokens=800)
    budget = 800 - int(800 * 0.25)

    prompt = assembler.fit_system_prompt(_sections())

    assert "Omitted this turn" in prompt
    assert assembler.count_tokens([{"role": "system", "content": prompt}]) <= budget


def test_an_oversized_prompt_no_longer_starves_the_conversation():
    """A pinned section is kept whole even when it alone exceeds the window, so
    the prompt can cost more than the entire budget. The history allowance was
    then whatever was left over — a negative number — and _trim_history returned
    nothing, so a bot with a 40k-token charter answered every message as if it
    were the first.

    The charter here is deliberately larger than the whole window, which is the
    only regime where this arithmetic decides anything: with a prompt that fits,
    the leftover is positive and the floor never applies. The request overflows
    either way in this state — the difference is a provider error naming the
    oversized charter versus a bot that quietly forgot what it was just told.
    """
    assembler = ContextAssembler(max_context_tokens=600)
    sections = [PromptSection("charter", "# Charter\n" + "long " * 700, pinned=True)]
    history = [
        {"role": "user", "content": "จำไว้ว่าชื่อโปรเจกต์คือ ORION"},
        {"role": "assistant", "content": "จำแล้วครับ"},
    ]

    messages = assembler.assemble(sections, history, {"role": "user", "content": "ชื่ออะไร"})

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "ORION" in messages[1]["content"]


class _SmallWindowConfig:
    """Enough of LLMConfigStore for the runtime's per-turn model resolution."""

    def __init__(self, window: int):
        self.window = window

    async def resolve(self, model_id, user_id=None, max_cost=None):
        return {
            "model_id": model_id,
            "api_key": "k",
            "api_base": None,
            "context_window": self.window,
        }

    async def default_model_for(self, max_cost=None):
        return "small-model"


@pytest.mark.asyncio
async def test_a_turn_on_a_small_model_is_packed_to_that_models_window(stores, tmp_path):
    """The assembler was built once from settings.llm.max_context_tokens, but the
    model is re-resolved every turn from the session picker and the admin's
    per-model rows. A turn on an 800-token model was still packed to the 60k
    default, so nothing was trimmed here and the provider rejected the request —
    the same conversation working on one model and failing on another, with no
    trimming in between.
    """
    user, _, session = await _bot_session(
        stores, "smallwindow@sbot.ai", charter="ตอบสั้นๆ", tool_allowlist=["web_search"]
    )
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.llm_config = _SmallWindowConfig(800)

    await runtime.handle_message(user.id, session.id, "สวัสดี", model="small-model")

    prompt = system_text(provider.calls[0])
    assert runtime.assembler.count_tokens([{"role": "system", "content": prompt}]) <= 800
    # The charter is pinned, so trimming is visible rather than total.
    assert "ตอบสั้นๆ" in prompt


def test_a_window_above_the_operators_budget_does_not_raise_it():
    """settings.llm.max_context_tokens is the operator's deliberate prompt
    budget, not a guess at the model's size — a 200k model must not silently
    overrule it."""
    runtime_assembler = ContextAssembler(max_context_tokens=60_000)
    picked = AgentRuntime._assembler_for(
        SimpleNamespace(assembler=runtime_assembler), 200_000
    )

    assert picked is runtime_assembler


@pytest.mark.asyncio
async def test_restricting_a_bots_tools_is_not_a_one_way_door(stores):
    """PATCH dropped every null in the body, so the value that means "back to
    unrestricted" was the one value the route could not send. Since the
    allowlist is enforced on every turn, a bot restricted once stayed
    restricted — and `[]` is no substitute, it means no tools at all."""
    user, bot, _ = await _bot_session(stores, "clear@sbot.ai", tool_allowlist=["web_search"])
    state = SimpleNamespace(bots=stores["bots"])

    await update_bot(bot.id, UpdateBotBody(tool_allowlist=None), user=user, state=state)

    assert (await stores["bots"].get(bot.id, user.id)).tool_allowlist is None


@pytest.mark.asyncio
async def test_a_field_left_out_of_the_patch_body_is_left_alone(stores):
    """The other half of the same distinction: honouring nulls must not turn
    every unsent field into a clear."""
    user, bot, _ = await _bot_session(stores, "keep@sbot.ai", tool_allowlist=["web_search"])
    state = SimpleNamespace(bots=stores["bots"])

    await update_bot(bot.id, UpdateBotBody(name="Renamed"), user=user, state=state)

    stored = await stores["bots"].get(bot.id, user.id)
    assert stored.name == "Renamed"
    assert stored.tool_allowlist == ["web_search"]


@pytest.mark.asyncio
async def test_a_skill_whose_connector_is_disabled_does_not_break_the_turn(
    stores, db_factory, tmp_path
):
    """resolve_tool_names answers None for a connector that is not currently
    connected, and a skill keeps its connector_id while the connector is only
    disabled. Filtering that None by the bot's allowlist raised TypeError inside
    the turn, so the user got a generic failure instead of an answer — the skill
    simply has no live tools to name, which is not an error."""
    skills = SkillStore(db_factory)
    user = await stores["users"].get_or_create_by_email("disconnected@sbot.ai")
    await skills.upsert(user.id, "gmail-triage", description="จัดกล่องจดหมาย", connector_id="c-gone")
    session = await stores["sessions"].create(user.id, title="chat", kind="direct")
    provider = FakeProvider([text_turn("ได้ครับ")])
    runtime = make_runtime(
        stores, provider, tmp_path, skills=skills, connectors=_DisconnectedConnectors()
    )

    await runtime.handle_message(user.id, session.id, "ช่วยจัดอีเมล")

    assert provider.calls, "the turn never reached the model"
    # Named as a skill, but with no tool names attached to it.
    assert "gmail-triage" in system_text(provider.calls[0])


class _EchoingConfig:
    """Resolves any model id to itself, and defaults to something else — so the
    model a turn ran on says which of the two the runtime picked."""

    async def resolve(self, model_id, user_id=None, max_cost=None):
        return {"model_id": model_id, "api_key": "k", "api_base": None, "context_window": 60_000}

    async def default_model_for(self, max_cost=None):
        return "admin-default"


@pytest.mark.asyncio
async def test_a_bots_configured_model_is_used_when_the_user_chats_with_it_directly(
    stores, tmp_path
):
    """A bot's model is part of its profile and was honoured on the delegate and
    mission paths, but direct chat only ever looked at the session's sticky
    choice — so the same specialist answered on a different model depending on
    who asked it."""
    user, _, session = await _bot_session(
        stores, "botmodel@sbot.ai", charter="ตอบสั้นๆ", model="bot-chosen-model"
    )
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.llm_config = _EchoingConfig()

    await runtime.handle_message(user.id, session.id, "สวัสดี")

    assert provider.models[0] == "bot-chosen-model"


@pytest.mark.asyncio
async def test_the_users_own_model_choice_still_beats_the_bots(stores, tmp_path):
    """The bot's model is its default, not a lock: the chat's model picker is a
    deliberate act by the user and has to win."""
    user, _, session = await _bot_session(
        stores, "botmodel-override@sbot.ai", charter="ตอบสั้นๆ", model="bot-chosen-model"
    )
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.llm_config = _EchoingConfig()

    await runtime.handle_message(user.id, session.id, "หนึ่ง", model="user-picked-model")
    # ...and it sticks for the rest of the conversation, without being re-passed.
    await runtime.handle_message(user.id, session.id, "สอง")

    assert provider.models == ["user-picked-model", "user-picked-model"]
