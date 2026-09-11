"""A specialist's skills and memory follow it off the chat path (PRD §4.5 / goal G4).

`ALWAYS_AVAILABLE_TOOLS` declares that `read_skill`, `remember` and
`recall_memory` are not capabilities an allowlist may take away, but
`SpecialistRunner` never built them — so the guarantee held only in direct
chat. A mission node, the one place a bot works unsupervised and the only place
that produces the outcome data self-improvement has to learn from, could
neither open the skill it was assigned nor write down what it learned.
"""

from types import SimpleNamespace

import pytest

from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.memory import MemoryService
from sbot.core.missions import MissionService
from sbot.core.specialist import SpecialistRunner
from sbot.db.stores import LLMConfigStore, SkillStore
from sbot.providers.base import ChatResult, ProviderError, TextDelta, ToolCall
from sbot.sandbox.ephemeral import EphemeralSandbox
from sbot.tools.cos import DelegateTool
from sbot.tools.skills import scope_skills
from tests.sbot_mode.conftest import FakeProvider, text_turn


def system_text(call: list[dict]) -> str:
    return next(m["content"] for m in call if m["role"] == "system")


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )


def make_memory(stores, provider) -> MemoryService:
    return MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)


@pytest.mark.asyncio
async def test_specialist_uses_control_plane_fallback(db_factory, tmp_path):
    llm_config = LLMConfigStore(db_factory)
    primary_provider = await llm_config.create_provider(
        "primary", "primary-key", "", model_prefix="openai"
    )
    fallback_provider = await llm_config.create_provider(
        "fallback", "fallback-key", "", model_prefix="openai"
    )
    primary = await llm_config.create_model(
        primary_provider.id, "openai/primary", "Primary", True, "low", "", kind="chat"
    )
    backup = await llm_config.create_model(
        fallback_provider.id, "openai/backup", "Backup", True, "low", "", kind="chat"
    )
    await llm_config.update_model(backup.id, owner_id=None, is_fallback=True)
    await llm_config.update_model(backup.id, owner_id=None, is_fallback=True)
    assert await llm_config.fallback_model_for(None) == "openai/backup"

    await llm_config.update_provider(fallback_provider.id, owner_id=None, enabled=False)
    assert await llm_config.fallback_model_for(None) is None
    await llm_config.update_provider(fallback_provider.id, owner_id=None, enabled=True)
    await llm_config.update_model(backup.id, owner_id=None, is_fallback=True)

    class FailingPrimary(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"] == "openai/primary":
                raise ProviderError("provider unavailable")
            yield TextDelta(text="specialist backup result")
            yield ChatResult(content="specialist backup result")

    provider = FailingPrimary([])
    runner = SpecialistRunner(
        provider=provider,
        sandbox=EphemeralSandbox(SandboxSettings(enabled=False)),
        workspace=tmp_path,
        model="openai/primary",
        llm_config=llm_config,
        llm_settings=LLMSettings(max_iterations=3),
    )
    bot = SimpleNamespace(
        id="bot-1", kind="specialist", model="openai/primary", tool_allowlist=[], skill_ids=[]
    )

    outcome = await runner.run(bot, "Complete the task", "Do it", lambda _event: None, "turn-1")

    assert outcome.text == "specialist backup result"
    assert provider.models == ["openai/primary", "openai/backup"]

    await llm_config.update_model(primary.id, owner_id=None, enabled=False)
    provider.models.clear()
    outcome = await runner.run(bot, "Complete the task", "Do it again", lambda _event: None, "turn-2")
    assert outcome.text == "specialist backup result"
    assert provider.models == ["openai/backup"]


def test_skill_scope_distinguishes_all_selected_and_none():
    """The editor persists these three values, so their prompt scope must stay distinct."""
    market = SimpleNamespace(id="market-id", name="market-research")
    payroll = SimpleNamespace(id="payroll-id", name="payroll")
    available = [market, payroll]

    assert scope_skills(available, None) == available
    assert scope_skills(available, [market.id]) == [market]
    assert scope_skills(available, []) == []


def make_service(stores, provider, tmp_path, skills=None, memory=None) -> MissionService:
    return MissionService(
        stores["missions"],
        stores["bots"],
        require_verified_results=False,  # These fixtures test context, not acceptance reporting.
        provider=provider,
        sandbox=None,
        settings=make_settings(tmp_path),
        skills=skills,
        memory=memory,
    )


async def _scoped_specialist(stores, skills, email: str):
    """A bot curated onto one of the owner's two skills."""
    user = await stores["users"].get_or_create_by_email(email)
    market = await skills.upsert(user.id, "market-research", description="หาขนาดตลาด")
    await skills.upsert(user.id, "payroll", description="คำนวณเงินเดือน")
    bot = await stores["bots"].create(
        owner_id=user.id,
        name="Researcher",
        role_title="Analyst",
        charter="หาข้อมูล",
        skill_ids=[market.id],
    )
    return user, bot


@pytest.mark.asyncio
async def test_a_mission_node_is_told_about_the_skills_it_was_assigned(
    stores, db_factory, tmp_path
):
    skills = SkillStore(db_factory)
    user, bot = await _scoped_specialist(stores, skills, "node-skills@sbot.ai")
    provider = FakeProvider([text_turn("ตลาดโต 12%")])
    service = make_service(stores, provider, tmp_path, skills=skills)

    mission = await service.plan(
        user.id,
        "ทำรายงานตลาด",
        [{"id": "a", "title": "หาข้อมูล", "bot_id": bot.id, "instruction": "หาขนาดตลาด"}],
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    prompt = system_text(provider.calls[0])
    assert "market-research" in prompt
    # Scoped, not just dumped: the point of skill_ids is that a narrow
    # specialist's prompt is not filled with instructions for other people's work.
    assert "payroll" not in prompt
    # And it must be able to actually open the skill it was just told about.
    assert "read_skill" in provider.offered_tools[0]


@pytest.mark.asyncio
async def test_a_mission_node_can_write_to_its_own_memory_and_read_it_back(
    stores, db_factory, tmp_path
):
    """The write half is what per-bot learning needs; the read half is what
    makes it learning rather than an append-only diary."""
    user = await stores["users"].get_or_create_by_email("node-memory@sbot.ai")
    bot = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", charter="หาข้อมูล"
    )
    provider = FakeProvider([
        [
            ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="m1",
                        name="remember",
                        arguments={
                            "action": "save",
                            "fact": "แหล่งข้อมูลตลาดที่เชื่อถือได้คือ Statista",
                        },
                    )
                ],
            )
        ],
        text_turn("จดไว้แล้ว และหาข้อมูลเสร็จ"),
    ])
    memory = make_memory(stores, provider)
    service = make_service(stores, provider, tmp_path, memory=memory)

    first = await service.plan(
        user.id,
        "หาข้อมูล",
        [{"id": "a", "title": "หา", "bot_id": bot.id, "instruction": "หาขนาดตลาด"}],
    )
    assert await service.run_to_completion(first.id, user.id) == "completed"

    tool_results = [m for m in provider.calls[1] if m.get("role") == "tool"]
    assert len(tool_results) == 1
    assert "not available" not in tool_results[0]["content"]
    # Written to the bot's OWN document, not the shared one — that separation is
    # what makes each specialist get better at its own role.
    own = await stores["memories"].get_core(user.id, scope="bot", scope_id=bot.id)
    assert "Statista" in own
    assert "Statista" not in await stores["memories"].get_core(user.id)

    # A second mission must start with that lesson in front of it.
    provider.turns = [text_turn("ใช้ Statista ตามที่จดไว้")]
    provider.calls.clear()
    provider.offered_tools.clear()
    second = await service.plan(
        user.id,
        "หาข้อมูลอีกรอบ",
        [{"id": "a", "title": "หา", "bot_id": bot.id, "instruction": "หาขนาดตลาดปีถัดไป"}],
    )
    assert await service.run_to_completion(second.id, user.id) == "completed"
    assert "Statista" in system_text(provider.calls[0])


@pytest.mark.asyncio
async def test_a_mission_node_cannot_overwrite_the_sessions_plan(stores, db_factory, tmp_path):
    """`update_plan` writes the *session's* plan, and a node has no session at
    all — a delegated specialist would be writing over Chief of Staff's."""
    user = await stores["users"].get_or_create_by_email("node-plan@sbot.ai")
    bot = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", charter="หาข้อมูล"
    )
    provider = FakeProvider([text_turn("เสร็จ")])
    service = make_service(
        stores, provider, tmp_path, skills=SkillStore(db_factory), memory=make_memory(stores, provider)
    )

    mission = await service.plan(
        user.id, "งาน", [{"id": "a", "title": "a", "bot_id": bot.id, "instruction": "ทำ"}]
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"

    assert "update_plan" not in provider.offered_tools[0]


@pytest.mark.asyncio
async def test_a_delegated_specialist_gets_its_skills_and_memory_too(
    stores, db_factory, tmp_path
):
    skills = SkillStore(db_factory)
    user, bot = await _scoped_specialist(stores, skills, "delegate-context@sbot.ai")
    provider = FakeProvider([text_turn("หาข้อมูลเสร็จแล้วครับ")])
    tool = DelegateTool(
        bot_store=stores["bots"],
        owner_id=user.id,
        provider=provider,
        sandbox=None,
        workspace=tmp_path / "ws",
        skills=skills,
        memory=make_memory(stores, provider),
    )

    await tool.execute(task="หาขนาดตลาด", bot_id=bot.id)

    prompt = system_text(provider.calls[0])
    assert "market-research" in prompt
    assert "payroll" not in prompt
    assert {"read_skill", "remember", "recall_memory"} <= set(provider.offered_tools[0])


@pytest.mark.asyncio
async def test_an_allowlist_cannot_take_away_a_specialists_own_memory(
    stores, db_factory, tmp_path
):
    """A bot restricted to research is restricted in what it can do to the
    world, not in whether it is allowed to remember doing it."""
    user = await stores["users"].get_or_create_by_email("delegate-allowlist@sbot.ai")
    bot = await stores["bots"].create(
        owner_id=user.id,
        name="Researcher",
        role_title="Analyst",
        charter="หาข้อมูล",
        tool_allowlist=["web_search"],
    )
    provider = FakeProvider([text_turn("ok")])
    tool = DelegateTool(
        bot_store=stores["bots"],
        owner_id=user.id,
        provider=provider,
        sandbox=None,
        workspace=tmp_path / "ws",
        skills=SkillStore(db_factory),
        memory=make_memory(stores, provider),
    )

    await tool.execute(task="หาข้อมูล", bot_id=bot.id)

    offered = set(provider.offered_tools[0])
    assert {"remember", "recall_memory", "read_skill"} <= offered
    assert offered.isdisjoint({"exec", "write_file", "edit_file"})


@pytest.mark.asyncio
async def test_the_runtime_actually_hands_delegate_the_skills_and_memory(
    stores, db_factory, tmp_path
):
    """The `delegate` tool only carries a specialist's context if the runtime
    that constructs it passes the stores in. Built directly, the tool looks
    correct while the wiring that production uses is missing — so this goes
    through a real Chief-of-Staff turn instead."""
    from sbot.core.bus import EventBus
    from sbot.core.runtime import AgentRuntime
    from sbot.providers.base import ChatResult as _ChatResult

    skills = SkillStore(db_factory)
    user, bot = await _scoped_specialist(stores, skills, "runtime-delegate@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    session = await stores["sessions"].create(
        user.id, title="delegate", bot_id=cos.id, kind="direct"
    )
    provider = FakeProvider([
        [
            _ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="d1",
                        name="delegate",
                        arguments={"bot_id": bot.id, "task": "หาขนาดตลาด"},
                    )
                ],
            )
        ],
        text_turn("ตลาดโต 12%"),
        text_turn("นักวิจัยรายงานว่าตลาดโต 12% ครับ"),
    ])
    runtime = AgentRuntime(
        settings=make_settings(tmp_path),
        provider=provider,
        bus=EventBus(),
        users=stores["users"],
        bots=stores["bots"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=make_memory(stores, provider),
        audit=stores["audit"],
        skills=skills,
    )

    await runtime.handle_message(user.id, session.id, "ให้นักวิจัยหาข้อมูล")

    # calls[1] is the specialist's own turn, nested inside the delegation.
    specialist_prompt = system_text(provider.calls[1])
    assert "Researcher" in specialist_prompt, "calls[1] is not the specialist's turn"
    assert "market-research" in specialist_prompt
    assert "payroll" not in specialist_prompt


@pytest.mark.asyncio
async def test_specialist_shell_deliverable_is_returned_as_an_artifact(tmp_path):
    """A delegated bot must give its parent a download path for files made by
    shell commands, not only for files made through write_file.

    The nested AgentLoop previously omitted its workspace, so this was the
    exact path that produced a file on disk but no attachment in group chat.
    """
    provider = FakeProvider([
        [ChatResult(content=None, tool_calls=[ToolCall(
            id="write-report", name="exec", arguments={"command": "printf report > market-report.docx"}
        )])],
        text_turn("รายงานเสร็จแล้ว"),
    ])
    runner = SpecialistRunner(
        provider=provider,
        sandbox=EphemeralSandbox(SandboxSettings(enabled=False)),
        workspace=tmp_path,
    )

    class Bot:
        id = "specialist"
        kind = "specialist"
        tool_allowlist = ["exec"]
        model = None

    outcome = await runner.run(Bot(), "Create the requested file.", "Create a report.", lambda _event: None, "t1")

    assert (tmp_path / "market-report.docx").is_file()
    assert outcome.artifacts == ["market-report.docx"]


def test_each_specialist_substep_gets_its_own_number():
    """The Execution panel keys substeps by index, so an unnumbered one collapses
    every tool the specialist runs onto the same row — a four-tool research task
    renders as one row rewriting itself instead of four steps."""
    from sbot.core.events import ToolFinished, ToolStarted
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(seen.append, "นักวิจัย")

    for tool in ("web_search", "web_fetch"):
        emit(ToolStarted(turn_id="t", tool=tool, args_preview=""))
        emit(ToolFinished(turn_id="t", tool=tool, result_preview="ok"))

    assert [(e["index"], e["status"]) for e in seen] == [
        (1, "running"),
        (1, "done"),
        (2, "running"),
        (2, "done"),
    ]


def _card_payloads(seen: list[dict]) -> list[dict]:
    """Only what was reported under an assignment card, not the Execution panel."""
    return [e for e in seen if e.get("kind")]


def test_a_specialists_tools_are_reported_under_its_own_assignment():
    """Without the delegation tag there is no way back from a step to the bot
    that ran it — `delegate_many` puts several of them on screen at once."""
    from sbot.core.events import ToolFinished, ToolStarted
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(
        seen.append, "นักวิจัย", delegation={"delegation_id": "d1", "bot_id": "b1"}
    )

    emit(ToolStarted(turn_id="t", tool="web_search", args_preview='{"query":"softnix"}'))
    emit(ToolFinished(turn_id="t", tool="web_search", result_preview="3 results"))

    steps = _card_payloads(seen)
    assert [s["kind"] for s in steps] == ["delegation_step", "delegation_step"]
    assert all(s["delegation_id"] == "d1" and s["bot_id"] == "b1" for s in steps)
    # One row per step: the start shows the arguments, the finish replaces them
    # with the result, and both carry the same index so the card can pair them.
    assert [s["index"] for s in steps] == [1, 1]
    assert steps[0]["detail"] == '{"query":"softnix"}'
    assert steps[0]["status"] == "running"
    assert steps[1]["detail"] == "3 results"
    assert steps[1]["status"] == "done"


def test_a_failed_tool_is_marked_on_the_card():
    from sbot.core.events import ToolFinished
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(
        seen.append, "นักวิจัย", delegation={"delegation_id": "d1", "bot_id": "b1"}
    )
    emit(ToolFinished(turn_id="t", tool="web_fetch", result_preview="boom", is_error=True))

    assert _card_payloads(seen)[0]["status"] == "error"


def test_a_specialists_reply_is_previewed_as_it_is_written():
    """The point of the card: a 58-second delegation used to be a spinner and
    nothing else."""
    from sbot.core.events import TextDeltaEvent
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(
        seen.append, "นักวิจัย", delegation={"delegation_id": "d1", "bot_id": "b1"}
    )

    for _ in range(30):
        emit(TextDeltaEvent(turn_id="t", text="ก" * 10))
    emit.flush()

    deltas = [e for e in _card_payloads(seen) if e["kind"] == "delegation_delta"]
    assert deltas, "the specialist's text never reached its card"
    # Equality, not containment: buffered text that no flush ever reaches is
    # dropped, and a substring check passes just as happily on two thirds of it.
    assert "".join(d["text"] for d in deltas) == "ก" * 300
    # Coalesced, not per token: four specialists streaming through one bounded
    # session queue evict each other's tool events.
    assert len(deltas) < 30


def test_a_reply_too_short_to_flush_still_reaches_the_card():
    """A specialist that answers in one short message and reaches for no tool
    trips neither flush, so its card showed a spinner and then an answer with
    no working-out in between — which is the whole point of the card."""
    from sbot.core.events import TextDeltaEvent
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(
        seen.append, "นักวิจัย", delegation={"delegation_id": "d1", "bot_id": "b1"}
    )

    emit(TextDeltaEvent(turn_id="t", text="ตลาดโต 12% ครับ"))
    assert _card_payloads(seen) == []

    emit.flush()

    assert [(e["kind"], e["text"]) for e in _card_payloads(seen)] == [
        ("delegation_delta", "ตลาดโต 12% ครับ")
    ]
    # Nothing left to send twice.
    emit.flush()
    assert len(_card_payloads(seen)) == 1


def test_a_short_reply_still_reaches_the_card_before_a_tool_runs():
    """Text written before reaching for a tool is the specialist explaining why
    — it would otherwise sit in the buffer waiting for a flush that never comes."""
    from sbot.core.events import TextDeltaEvent, ToolStarted
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(
        seen.append, "นักวิจัย", delegation={"delegation_id": "d1", "bot_id": "b1"}
    )

    emit(TextDeltaEvent(turn_id="t", text="ขอค้นหาก่อนนะครับ"))
    emit(ToolStarted(turn_id="t", tool="web_search", args_preview=""))

    kinds = [e["kind"] for e in _card_payloads(seen)]
    assert kinds == ["delegation_delta", "delegation_step"]


def test_an_untagged_specialist_still_never_streams_its_text():
    """A mission node has no assignment card to report into. Its text must stay
    dropped: untagged, it lands in the delegating bot's own reply and the two
    voices interleave — which is why this bridge dropped text outright."""
    from sbot.core.events import TextDeltaEvent
    from sbot.core.specialist import specialist_progress

    seen: list[dict] = []
    emit = specialist_progress(seen.append, "นักวิจัย")

    for _ in range(50):
        emit(TextDeltaEvent(turn_id="t", text="ก" * 10))

    assert seen == []


def test_two_specialists_number_their_own_steps_from_one():
    """The card lists one bot's work, so its steps count from 1 whoever else is
    running — unlike the Execution panel's shared index."""
    import itertools

    from sbot.core.events import ToolStarted
    from sbot.core.specialist import specialist_progress

    shared = itertools.count(1)
    seen: list[dict] = []
    first = specialist_progress(
        seen.append, "นักวิจัย", lambda: next(shared), {"delegation_id": "d1", "bot_id": "b1"}
    )
    second = specialist_progress(
        seen.append, "นักกลยุทธ์", lambda: next(shared), {"delegation_id": "d2", "bot_id": "b2"}
    )

    first(ToolStarted(turn_id="t", tool="web_search", args_preview=""))
    second(ToolStarted(turn_id="t", tool="read_file", args_preview=""))

    cards = _card_payloads(seen)
    assert [(c["delegation_id"], c["index"]) for c in cards] == [("d1", 1), ("d2", 1)]
    # The panel's own numbering still has to differ, or one bot's row overwrites
    # the other's.
    panel = [e for e in seen if not e.get("kind")]
    assert [p["index"] for p in panel] == [1, 2]
