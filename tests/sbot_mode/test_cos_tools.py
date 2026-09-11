"""Tests for Chief of Staff specialized tools (create_bot, list_bots, delegate)."""

import asyncio
import json
import pytest

from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.memory import MemoryService
from sbot.core.runtime import _STORED_TOOL_RESULT_CAP, AgentRuntime
from sbot.providers.base import ChatResult, ToolCall
from tests.sbot_mode.conftest import FakeProvider, text_turn


def tool_call_turn(name: str, args: dict, call_id: str = "c1"):
    return [ChatResult(content=None, tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])]


async def _delegate_tool(stores, tmp_path, email: str):
    """A `delegate` tool with one specialist behind it, for the tests that drive
    the tool directly to control exactly how the nested run ended."""
    from sbot.tools.cos import DelegateTool

    user = await stores["users"].get_or_create_by_email(email)
    await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )
    return DelegateTool(stores["bots"], user.id, provider=None, sandbox=None, workspace=tmp_path)


def make_runtime(stores, provider, tmp_path) -> AgentRuntime:
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
    )


@pytest.mark.asyncio
async def test_cos_creates_bot_via_tool(stores, tmp_path):
    user = await stores["users"].get_or_create_by_email("cos_owner@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    session = await stores["sessions"].create(
        user.id, title="CoS Setup Team", bot_id=cos.id, kind="direct"
    )

    # Turn 1: CoS decides to call create_bot
    turn1_provider = FakeProvider([
        tool_call_turn(
            "create_bot",
            {
                "name": "นักวิเคราะห์การเงิน",
                "role_title": "Financial Analyst",
                "charter": "วิเคราะห์งบการเงินและตัวเลขทางธุรกิจ",
                "tool_allowlist": ["read_file", "exec"],
            },
            call_id="call_c1",
        ),
        text_turn("สร้างบอทนักวิเคราะห์การเงินเรียบร้อยแล้วครับหัวหน้า"),
    ])
    runtime = make_runtime(stores, turn1_provider, tmp_path)

    answer = await runtime.handle_message(user.id, session.id, "สร้างบอทการเงินให้หน่อย")
    assert "เรียบร้อยแล้ว" in answer
    # A request to create a member ends at creation. The model must not get a
    # second turn in which it might invent a smoke test or a work plan.
    assert len(turn1_provider.calls) == 1
    assert len(turn1_provider.turns) == 1

    # Verify bot is created in DB
    fin_bot = await stores["bots"].get_by_name(user.id, "นักวิเคราะห์การเงิน")
    assert fin_bot is not None
    assert fin_bot.role_title == "Financial Analyst"
    assert fin_bot.kind == "specialist"
    assert fin_bot.created_by == f"bot:{cos.id}"


@pytest.mark.asyncio
async def test_cos_creates_requested_roster_in_one_terminal_turn(stores, tmp_path):
    user = await stores["users"].get_or_create_by_email("cos_roster@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    session = await stores["sessions"].create(user.id, bot_id=cos.id, kind="direct")
    provider = FakeProvider([tool_call_turn("create_bots", {"bots": [
        {"name": "นักวิจัย", "role_title": "Researcher", "charter": "Find primary sources."},
        {"name": "นักวิเคราะห์", "role_title": "Analyst", "charter": "Synthesize evidence."},
        {"name": "นักเขียน", "role_title": "Academic Writer", "charter": "Write cited reports."},
    ]})])
    runtime = make_runtime(stores, provider, tmp_path)

    answer = await runtime.handle_message(user.id, session.id, "สร้างบอททั้งสามคน")

    assert "สร้างบอทครบ 3 คน" in answer
    assert len(provider.calls) == 1
    for name in ("นักวิจัย", "นักวิเคราะห์", "นักเขียน"):
        assert await stores["bots"].get_by_name(user.id, name) is not None


@pytest.mark.asyncio
async def test_create_bots_preflight_prevents_partial_roster(stores):
    from sbot.tools.cos import CreateBotsTool

    user = await stores["users"].get_or_create_by_email("cos_batch_invalid@sbot.ai")
    tool = CreateBotsTool(stores["bots"], user.id, creator_bot_id="cos")
    result = await tool.execute(bots=[
        {"name": "Researcher", "role_title": "Research", "charter": "Find sources."},
        {"name": "Researcher", "role_title": "Analyst", "charter": "Analyze sources."},
    ])

    assert "duplicate bot name" in result
    assert await stores["bots"].get_by_name(user.id, "Researcher") is None


@pytest.mark.asyncio
async def test_create_bots_existing_member_rolls_back_entire_roster(stores):
    """A valid first member must not survive a later duplicate failure."""
    from sbot.tools.cos import CreateBotsTool

    user = await stores["users"].get_or_create_by_email("cos_batch_existing@sbot.ai")
    await stores["bots"].create(owner_id=user.id, name="Existing", role_title="Existing")
    tool = CreateBotsTool(stores["bots"], user.id, creator_bot_id="cos")

    result = await tool.execute(bots=[
        {"name": "New member", "role_title": "Researcher", "charter": "Find sources."},
        {"name": "Existing", "role_title": "Analyst", "charter": "Analyse sources."},
    ])

    assert "already exists" in result
    assert await stores["bots"].get_by_name(user.id, "New member") is None


@pytest.mark.asyncio
async def test_concurrent_rosters_do_not_create_duplicate_active_members(stores):
    """The per-owner lock serializes competing turns before the DB backstop."""
    from sbot.tools.cos import CreateBotsTool

    user = await stores["users"].get_or_create_by_email("cos_batch_race@sbot.ai")
    first = CreateBotsTool(stores["bots"], user.id, creator_bot_id="cos")
    second = CreateBotsTool(stores["bots"], user.id, creator_bot_id="cos")
    roster = [
        {"name": "Researcher", "role_title": "Research", "charter": "Find sources."},
        {"name": "Analyst", "role_title": "Analysis", "charter": "Analyse sources."},
    ]

    outcomes = await asyncio.gather(first.execute(bots=roster), second.execute(bots=roster))

    assert sum("สร้างบอทครบ 2 คน" in outcome for outcome in outcomes) == 1
    assert sum(outcome.startswith("Error:") for outcome in outcomes) == 1
    assert len(await stores["bots"].list_for_user(user.id)) == 2


@pytest.mark.asyncio
async def test_creating_a_bot_skips_unsolicited_bundled_work(stores, tmp_path):
    """A model may emit several calls at once; creation must still be bounded."""
    user = await stores["users"].get_or_create_by_email("bounded_create@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    session = await stores["sessions"].create(user.id, bot_id=cos.id, kind="direct")
    provider = FakeProvider([[
        ChatResult(content=None, tool_calls=[
            ToolCall(id="create", name="create_bot", arguments={
                "name": "TOR", "role_title": "TOR specialist", "charter": "Draft TOR only.",
            }),
            ToolCall(id="unsolicited", name="delegate", arguments={
                "bot_name": "TOR", "task": "Run a smoke test and create a file.",
            }),
        ]),
    ]])
    runtime = make_runtime(stores, provider, tmp_path)

    answer = await runtime.handle_message(user.id, session.id, "สร้างบอท TOR")

    assert "สร้างบอท 'TOR' เรียบร้อยแล้ว" in answer
    assert await stores["bots"].get_by_name(user.id, "TOR") is not None
    assert len(provider.calls) == 1
    history = await stores["messages"].recent(session.id)
    assert not any("smoke test" in str(message.get("content")) for message in history)


@pytest.mark.asyncio
async def test_cos_delegates_to_specialist(stores, tmp_path):
    user = await stores["users"].get_or_create_by_email("cos_delegator@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    
    # Pre-create researcher specialist bot
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )

    session = await stores["sessions"].create(
        user.id, title="CoS Delegation Chat", bot_id=cos.id, kind="direct"
    )

    # Provider will simulate:
    # 1. CoS calls delegate tool with bot_name="นักวิจัย"
    # 2. Inside delegate tool, the specialist AgentLoop calls the provider and gets "พบข้อมูลน่าสนใจ 3 ข้อครับ"
    # 3. CoS receives tool result and produces final answer
    provider = FakeProvider([
        tool_call_turn(
            "delegate",
            {
                "bot_name": "นักวิจัย",
                "task": "ค้นหาข้อมูลคู่แข่ง 3 ราย",
            },
            call_id="call_del_1",
        ),
        text_turn("พบข้อมูลน่าสนใจ 3 ข้อครับ รายละเอียดคือ A, B, C"),
        text_turn("นักวิจัยได้รายงานผลเรียบร้อยแล้วครับ: พบข้อมูล 3 ข้อคือ A, B, C"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    answer = await runtime.handle_message(
        user.id, session.id, "ให้นักวิจัยหาข้อมูลคู่แข่งให้หน่อย"
    )
    assert "นักวิจัยได้รายงานผลเรียบร้อยแล้ว" in answer
    assert "A, B, C" in answer


@pytest.mark.asyncio
async def test_a_delegation_is_streamed_and_stored_as_the_specialist_speaking(stores, tmp_path):
    """Bot-to-bot work is the product, so the UI has to be able to render it as
    the specialist's own message — live and after a reload. The `delegate` tool
    result alone can't carry that: it is truncated to a debug preview on the
    wire and stored under a role the transcript endpoint drops."""
    user = await stores["users"].get_or_create_by_email("delegation_ui@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
        tool_allowlist=["exec"],
    )
    session = await stores["sessions"].create(
        user.id, title="Delegation UI", bot_id=cos.id, kind="direct"
    )

    # Longer than the 200-char preview the tool events are capped at, so a
    # truncated path can't pass this test.
    report = "รายงานผลการวิจัย: " + " ".join(["คู่แข่งรายที่หนึ่งมีส่วนแบ่งตลาดสูงมาก"] * 12)
    assert len(report) > 400
    # No surrounding whitespace, so this stays an assertion about the full text
    # surviving rather than about how the reply is trimmed.
    assert report == report.strip()
    provider = FakeProvider([
        tool_call_turn("delegate", {"bot_name": "นักวิจัย", "task": "ค้นหาข้อมูลคู่แข่ง 3 ราย"}),
        tool_call_turn("exec", {"command": "printf report > delegated-report.docx"}),
        text_turn(report),
        text_turn("สรุปให้ครับ"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    events = []
    publish = runtime.bus.publish
    runtime.bus.publish = lambda sid, ev: (events.append(ev), publish(sid, ev))[1]

    await runtime.handle_message(user.id, session.id, "ให้นักวิจัยหาข้อมูลคู่แข่งให้หน่อย")

    started = [e for e in events if e.type == "delegation_started"]
    finished = [e for e in events if e.type == "delegation_finished"]
    assert len(started) == 1 and len(finished) == 1
    # The resolved bot, not the name the model typed — the client needs an id to
    # pick an avatar with, and a role to label the bubble.
    assert started[0].bot_id == researcher.id
    assert started[0].bot_name == "นักวิจัย"
    assert started[0].role_title == "Senior Researcher"
    assert started[0].task == "ค้นหาข้อมูลคู่แข่ง 3 ราย"
    assert finished[0].bot_id == researcher.id
    assert finished[0].text == report
    assert finished[0].artifacts == ["delegated-report.docx"]
    assert finished[0].is_error is False

    page, _ = await stores["messages"].page_for_display(session.id)
    spoken = [m for m in page if m["speaker_bot_id"] == researcher.id]
    assert len(spoken) == 1
    assert spoken[0]["content"] == report
    assert spoken[0]["meta"]["delegated_task"] == "ค้นหาข้อมูลคู่แข่ง 3 ราย"
    assert spoken[0]["meta"]["artifacts"] == ["delegated-report.docx"]
    # Ordered before the leader's own closing message, so the transcript reads
    # in the order the work happened.
    assert page.index(spoken[0]) < next(
        i for i, m in enumerate(page) if m["content"] == "สรุปให้ครับ"
    )

    # Never fed back to the model: it already read this exact text as the
    # delegate tool's result, and it is not the assistant's own voice.
    history = await stores["messages"].recent(session.id)
    assert all(report not in (m.get("content") or "") for m in history if m["role"] == "assistant")


@pytest.mark.asyncio
async def test_a_specialist_reply_is_stored_after_the_words_that_introduced_it(stores, tmp_path):
    """The leader's "I'll ask X" rides on the same assistant message as the
    `delegate` call, so a specialist row stored ahead of it makes a reloaded
    conversation read backwards — the answer arriving before the handoff."""
    user = await stores["users"].get_or_create_by_email("delegation_order@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )
    session = await stores["sessions"].create(
        user.id, title="Delegation order", bot_id=cos.id, kind="direct"
    )

    provider = FakeProvider([
        [
            ChatResult(
                content="ผมจะให้นักวิจัยช่วยดูครับ",
                tool_calls=[
                    ToolCall(
                        id="call_del_1",
                        name="delegate",
                        arguments={"bot_name": "นักวิจัย", "task": "หาข้อมูลคู่แข่ง"},
                    )
                ],
            )
        ],
        text_turn("พบคู่แข่ง 3 ราย"),
        text_turn("สรุปให้ครับ"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ให้นักวิจัยหาข้อมูลให้หน่อย")

    page, _ = await stores["messages"].page_for_display(session.id)
    spoken = [m["content"] for m in page if m["role"] == "assistant"]
    assert spoken == ["ผมจะให้นักวิจัยช่วยดูครับ", "พบคู่แข่ง 3 ราย", "สรุปให้ครับ"]


@pytest.mark.asyncio
async def test_a_long_specialist_reply_is_marked_where_it_was_cut(stores, tmp_path):
    """The `delegate` tool row that holds the rest of the text is dropped by the
    transcript endpoint, so this row is the whole reply as far as a reader is
    concerned — and an unmarked cut reads as a specialist that gave up."""
    user = await stores["users"].get_or_create_by_email("delegation_cap@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )
    session = await stores["sessions"].create(
        user.id, title="Delegation cap", bot_id=cos.id, kind="direct"
    )

    report = "ก" * (_STORED_TOOL_RESULT_CAP + 500)
    provider = FakeProvider([
        tool_call_turn("delegate", {"bot_name": "นักวิจัย", "task": "เขียนรายงานยาว"}),
        text_turn(report),
        text_turn("สรุปให้ครับ"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ขอรายงานยาวๆ")

    page, _ = await stores["messages"].page_for_display(session.id)
    spoken = next(m for m in page if m["speaker_bot_id"] == researcher.id)
    assert spoken["content"] == report[:_STORED_TOOL_RESULT_CAP] + "\n... (truncated)"


@pytest.mark.asyncio
async def test_a_specialist_that_raises_still_closes_its_delegation(stores, tmp_path):
    """A nested agent loop can raise, and the tool registry turns that into an
    error string rather than letting it surface — so nothing else would ever
    close the handoff: the assignment card spins forever and the reply row is
    discarded for having no content."""
    from sbot.tools.cos import DelegateTool

    user = await stores["users"].get_or_create_by_email("delegation_raise@sbot.ai")
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )
    tool = DelegateTool(
        stores["bots"], user.id, provider=None, sandbox=None, workspace=tmp_path
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider stream ended without a result")

    tool.runner.run = boom

    seen: list[dict] = []
    with pytest.raises(RuntimeError):
        await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert [e["kind"] for e in seen] == ["delegation_started", "delegation_finished"]
    # Paired by id, not by bot: the same bot can be delegated to twice in a turn.
    assert seen[0]["delegation_id"] == seen[1]["delegation_id"]
    assert seen[1]["bot_id"] == researcher.id
    assert seen[1]["is_error"] is True
    assert seen[1]["text"]


@pytest.mark.asyncio
async def test_a_roster_too_long_to_list_says_so(stores, tmp_path):
    """The pinned roster is cut at a fixed count, oldest-first, so what drops
    off is whatever the user created most recently. A leader told only that
    `list_bots` returns fuller charters reads the short list as the whole team
    and concludes nobody fits."""
    from sbot.core.runtime import _TEAM_ROSTER_LIMIT

    user = await stores["users"].get_or_create_by_email("big_team@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    over = _TEAM_ROSTER_LIMIT + 2
    for i in range(over):
        await stores["bots"].create(
            owner_id=user.id, name=f"บอท{i}", role_title="Specialist", charter="ทำงาน"
        )
    session = await stores["sessions"].create(user.id, title="cos", bot_id=cos.id, kind="direct")
    provider = FakeProvider([text_turn("รับทราบครับ")])

    await make_runtime(stores, provider, tmp_path).handle_message(user.id, session.id, "สวัสดี")

    prompt = provider.calls[0][0]["content"]
    assert f"Showing {_TEAM_ROSTER_LIMIT} of {over}" in prompt
    assert "list_bots" in prompt
    # The ones it can see are still named — the notice is an addition, not a
    # replacement for the roster.
    assert "บอท0" in prompt


@pytest.mark.asyncio
async def test_a_leader_cannot_hand_the_work_back_to_itself(stores, tmp_path):
    """A self-delegation runs the leader's own turn nested inside itself, and
    the mirror then writes that nested run into the very session the user is
    watching: a fabricated user message, the nested stream inside the leader's
    own bubble, and a `turn_completed` that ends the real turn early.

    Every way of naming the leader, because the model chooses the spelling: the
    id, the name, and the near-match that exists to rescue a mistyped id.
    """
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateTool

    user = await stores["users"].get_or_create_by_email("self_delegation@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    await stores["bots"].create(
        owner_id=user.id, name="นักวิจัย", role_title="Researcher", charter="หาข้อมูล"
    )
    tool = DelegateTool(
        stores["bots"],
        user.id,
        provider=None,
        sandbox=None,
        workspace=tmp_path,
        leader_bot_id=cos.id,
    )
    ran: list[str] = []

    async def answered(bot, *_args, **_kwargs):
        ran.append(bot.id)
        return SpecialistOutcome(text="ok")

    tool.runner.run = answered

    replies = [
        await tool.execute(task="งาน", bot_id=cos.id),
        await tool.execute(task="งาน", bot_name=cos.name),
        # One character short of the leader's id — the path that resolves a
        # mistyped id has to reject it too, not helpfully find it.
        await tool.execute(task="งาน", bot_id=cos.id[:-1]),
    ]

    assert ran == []
    assert all(r.startswith("Error:") for r in replies)
    # And the specialist beside it is still reachable, so this is a refusal to
    # delegate to itself and not a refusal to delegate.
    assert not (await tool.execute(task="งาน", bot_name="นักวิจัย")).startswith("Error:")
    assert len(ran) == 1


@pytest.mark.asyncio
async def test_a_cancelled_delegation_still_closes_its_assignment(stores, tmp_path):
    """A Telegram worker restart cancels the task awaiting this turn, and
    `CancelledError` is not an `Exception` — so the card spun forever and the
    specialist's thread stayed claimed for the rest of the process."""
    from sbot.core.specialist import DelegationMirror
    from sbot.tools.cos import DelegateTool

    user = await stores["users"].get_or_create_by_email("delegation_cancel@sbot.ai")
    researcher = await stores["bots"].create(
        owner_id=user.id, name="นักวิจัย", role_title="Researcher", charter="หาข้อมูล"
    )
    bus = EventBus()
    mirror = DelegationMirror(bus, stores["sessions"], stores["messages"], user.id)
    tool = DelegateTool(
        stores["bots"],
        user.id,
        provider=None,
        sandbox=None,
        workspace=tmp_path,
        mirror=mirror,
    )

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    tool.runner.run = cancelled

    seen: list[dict] = []
    with pytest.raises(asyncio.CancelledError):
        await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert [e["kind"] for e in seen] == ["delegation_started", "delegation_finished"]
    assert seen[1]["is_error"] is True
    # A claim that outlives its run silences that bot's thread from then on.
    thread = await stores["sessions"].thread_for_bot(user.id, researcher.id, title=researcher.name)
    assert await mirror.open(researcher, "งานต่อไป", "del_2") == thread.id


@pytest.mark.asyncio
async def test_a_short_delegated_reply_is_still_previewed_on_its_card(stores, tmp_path):
    """Text is buffered until there is enough of it to be worth forwarding, so
    somebody has to say when no more is coming. A reply that filled less than
    one flush and reached for no tool along the way was never sent at all, and
    its card showed a spinner and then an answer with no working-out between."""
    from sbot.core.events import TextDeltaEvent
    from sbot.core.specialist import SpecialistOutcome

    tool = await _delegate_tool(stores, tmp_path, "delegation_shortreply@sbot.ai")

    async def brief(_bot, _system, _user, sink, **_kwargs):
        sink(TextDeltaEvent(turn_id="t1", text="สั้นมาก"))
        return SpecialistOutcome(text="สั้นมาก")

    tool.runner.run = brief

    seen: list[dict] = []
    await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert [e["text"] for e in seen if e["kind"] == "delegation_delta"] == ["สั้นมาก"]


async def _delegate_many_tool(stores, tmp_path, email: str):
    """A `delegate_many` tool with two specialists behind it."""
    from sbot.tools.cos import DelegateManyTool, DelegateTool

    user = await stores["users"].get_or_create_by_email(email)
    for name, role in (("นักวิจัย", "Senior Researcher"), ("นักกลยุทธ์", "Strategy Lead")):
        await stores["bots"].create(owner_id=user.id, name=name, role_title=role, charter="ทำงาน")
    delegate = DelegateTool(stores["bots"], user.id, provider=None, sandbox=None, workspace=tmp_path)
    return DelegateManyTool(delegate), delegate


@pytest.mark.asyncio
async def test_specialists_handed_work_together_actually_run_together(stores, tmp_path):
    """The whole point of the tool. The agent loop runs a turn's tool calls one
    after another, so two `delegate` calls meant to be simultaneous were not:
    the second specialist started only once the first had finished, on a clock
    that was paying for both. Each run here waits for the other to reach the
    same point, so anything sequential deadlocks instead of quietly passing."""
    from sbot.core.specialist import SpecialistOutcome

    tool, delegate = await _delegate_many_tool(stores, tmp_path, "delegation_parallel@sbot.ai")
    both_started = asyncio.Barrier(2)

    async def waits_for_the_other(bot, *_args, **_kwargs):
        await both_started.wait()
        return SpecialistOutcome(text=f"{bot.name} เสร็จแล้ว")

    delegate.runner.run = waits_for_the_other

    result = await asyncio.wait_for(
        tool.execute(
            assignments=[
                {"bot_name": "นักวิจัย", "task": "หาข้อมูล"},
                {"bot_name": "นักกลยุทธ์", "task": "วางแผน"},
            ]
        ),
        timeout=5,
    )

    assert "นักวิจัย เสร็จแล้ว" in result
    assert "นักกลยุทธ์ เสร็จแล้ว" in result


@pytest.mark.asyncio
async def test_concurrent_specialists_do_not_land_on_the_same_substep(stores, tmp_path):
    """All of them report into the one tool row that started them, and the panel
    keys a substep by its index alone — so with each bot numbering its own steps
    from 1, their rows overwrite each other and one bot finishing a step marks
    another bot's still-running step as done."""
    from sbot.core.specialist import SpecialistOutcome

    tool, delegate = await _delegate_many_tool(stores, tmp_path, "delegation_substeps@sbot.ai")
    both_started = asyncio.Barrier(2)

    class ToolStarted:
        type = "tool_started"
        tool = "web_search"

    async def reports_a_step(bot, _system, _user, emit, **_kwargs):
        await both_started.wait()
        emit(ToolStarted())
        return SpecialistOutcome(text=f"{bot.name} เสร็จแล้ว")

    delegate.runner.run = reports_a_step

    seen: list[dict] = []
    await asyncio.wait_for(
        tool.execute(
            assignments=[
                {"bot_name": "นักวิจัย", "task": "หาข้อมูล"},
                {"bot_name": "นักกลยุทธ์", "task": "วางแผน"},
            ],
            progress=seen.append,
        ),
        timeout=5,
    )

    steps = [payload for payload in seen if payload.get("stage") == "step"]
    assert len(steps) == 2
    assert len({payload["index"] for payload in steps}) == 2


@pytest.mark.asyncio
async def test_one_failed_assignment_does_not_discard_the_others_work(stores, tmp_path):
    """A nested loop can raise, and a raise out of the gather would throw away
    replies from specialists that already finished — work the turn has paid for
    and the leader needs to report anything at all."""
    from sbot.core.specialist import SpecialistOutcome

    tool, delegate = await _delegate_many_tool(stores, tmp_path, "delegation_partial@sbot.ai")

    async def one_of_them_breaks(bot, *_args, **_kwargs):
        if bot.name == "นักวิจัย":
            raise RuntimeError("provider stream ended without a result")
        return SpecialistOutcome(text="วางแผนเสร็จแล้ว")

    delegate.runner.run = one_of_them_breaks

    result = await tool.execute(
        assignments=[
            {"bot_name": "นักวิจัย", "task": "หาข้อมูล"},
            {"bot_name": "นักกลยุทธ์", "task": "วางแผน"},
        ]
    )

    assert "วางแผนเสร็จแล้ว" in result
    # Named, so the leader can retry the one that broke rather than all of them.
    assert "นักวิจัย" in result and "provider stream ended" in result


@pytest.mark.asyncio
async def test_a_fan_out_larger_than_the_cap_runs_nothing(stores, tmp_path):
    """Every assignment is a whole nested agent loop against one provider and
    one workspace, so a leader asking for a dozen at once is a runaway, not a
    plan — and it has to be refused before any of them start spending."""
    from sbot.tools.cos import MAX_PARALLEL_DELEGATIONS

    tool, delegate = await _delegate_many_tool(stores, tmp_path, "delegation_fanout@sbot.ai")
    ran: list[str] = []

    async def record(bot, *_args, **_kwargs):
        ran.append(bot.name)
        raise AssertionError("should never start")

    delegate.runner.run = record

    result = await tool.execute(
        assignments=[
            {"bot_name": "นักวิจัย", "task": f"งานที่ {i}"}
            for i in range(MAX_PARALLEL_DELEGATIONS + 1)
        ]
    )

    assert result.startswith("Error:")
    assert ran == []


@pytest.mark.asyncio
async def test_a_delegation_survives_a_bot_id_the_model_mistyped(stores, tmp_path):
    """A bot id is 32 random hex characters the model copies out of `list_bots`
    by hand. Dropping one of them used to end the delegation outright, and the
    leader then did the specialist's work itself and told the user the bot was
    broken — so a near-miss has to resolve to the only bot it can mean."""
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateTool

    user = await stores["users"].get_or_create_by_email("delegation_typo@sbot.ai")
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลเชิงลึก",
    )
    tool = DelegateTool(stores["bots"], user.id, provider=None, sandbox=None, workspace=tmp_path)

    async def answered(*_args, **_kwargs):
        return SpecialistOutcome(text="พบข้อมูลแล้ว")

    tool.runner.run = answered

    mistyped = researcher.id[:20] + researcher.id[21:]
    seen: list[dict] = []
    result = await tool.execute(task="หาข้อมูล", bot_id=mistyped, progress=seen.append)

    assert "พบข้อมูลแล้ว" in result
    # The card names the bot that actually ran, not the id the model typed.
    assert seen[0]["bot_id"] == researcher.id


@pytest.mark.asyncio
async def test_a_wrong_bot_id_falls_through_to_the_name_beside_it(stores, tmp_path):
    """The model often sends both fields, and the id was checked instead of the
    name rather than before it — so one wrong character discarded a name that
    was right there and would have resolved."""
    from sbot.core.specialist import SpecialistOutcome

    tool = await _delegate_tool(stores, tmp_path, "delegation_bothfields@sbot.ai")

    async def answered(*_args, **_kwargs):
        return SpecialistOutcome(text="พบข้อมูลแล้ว")

    tool.runner.run = answered

    result = await tool.execute(task="หาข้อมูล", bot_id="not-a-real-id", bot_name="นักวิจัย")
    assert "พบข้อมูลแล้ว" in result


@pytest.mark.asyncio
async def test_an_id_close_to_two_bots_is_not_guessed_at(stores, tmp_path):
    """Delegating to the wrong specialist is worse than not delegating: it
    spends a whole nested run and returns work nobody asked that bot for. A
    near-miss only resolves when there is exactly one bot it can mean."""
    tool = await _delegate_tool(stores, tmp_path, "delegation_ambiguous@sbot.ai")

    class FakeBot:
        def __init__(self, bot_id: str, name: str):
            self.id = bot_id
            self.name = name

    twins = [FakeBot("a" * 32, "แฝดหนึ่ง"), FakeBot("a" * 31 + "c", "แฝดสอง")]

    class FakeStore:
        async def get(self, *_args, **_kwargs):
            return None

        async def get_by_name(self, *_args, **_kwargs):
            return None

        async def list_for_user(self, *_args, **_kwargs):
            return twins

    tool.bot_store = FakeStore()

    result = await tool.execute(task="หาข้อมูล", bot_id="a" * 31 + "b")
    assert result.startswith("Error:")
    # Both candidates are named, so the retry needs no extra lookup round-trip.
    assert "แฝดหนึ่ง" in result and "แฝดสอง" in result


@pytest.mark.asyncio
async def test_a_specialist_cut_off_by_its_budget_does_not_report_success(stores, tmp_path):
    """A specialist cut off mid-stream returns no text at all. Calling that
    "completed the task" told the delegating model the run had succeeded, so it
    redid the whole task itself — doubling a turn whose budget was already
    spent. The result has to name the timeout and say not to repeat the work."""
    from sbot.core.specialist import SpecialistOutcome

    tool = await _delegate_tool(stores, tmp_path, "delegation_timeout@sbot.ai")

    async def cut_off(*_args, **_kwargs):
        return SpecialistOutcome(text="", timed_out=True)

    tool.runner.run = cut_off

    seen: list[dict] = []
    result = await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert seen[1]["is_error"] is True
    assert "completed" not in seen[1]["text"]
    assert "หมดเวลา" in result
    assert "อย่าลงมือทำงานเดิมซ้ำเอง" in result


@pytest.mark.asyncio
async def test_a_specialist_that_runs_out_of_steps_is_not_reported_as_success(stores, tmp_path):
    """Running out of provider rounds ends the nested loop with no answer just
    like running out of time does, and only `timed_out` was being checked — so
    the exhausted case still reached the leader wearing the success header, the
    exact misreport that made it redo the whole task."""
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateTool

    tool = await _delegate_tool(stores, tmp_path, "delegation_steps@sbot.ai")

    async def out_of_steps(*_args, **_kwargs):
        return SpecialistOutcome(text="", reached_max_iterations=True)

    tool.runner.run = out_of_steps

    seen: list[dict] = []
    result = await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert seen[1]["is_error"] is True
    assert "completed" not in seen[1]["text"]
    # The stand-in names the limit that ran out too — telling the user it ran
    # out of time sends them to shrink a budget that was never the problem.
    assert "step limit" in seen[1]["text"]
    assert "ran out of time" not in seen[1]["text"]
    assert "ยังทำไม่เสร็จ" in result
    # Names the limit that actually stopped it, so the leader can narrow the
    # task rather than wait for time it was never short of.
    assert "ใช้ขั้นตอนครบจำนวน" in result
    assert "อย่าลงมือทำงานเดิมซ้ำเอง" in result


@pytest.mark.asyncio
async def test_the_do_not_redo_note_survives_a_reply_long_enough_to_be_truncated(stores, tmp_path):
    """The note is the whole point of the cut-off result, and tool results are
    head-truncated on the way to the model — so appending it after a long
    partial report deleted it and substituted the truncation footer's advice to
    re-run the tool, which is the opposite instruction."""
    from sbot.core.loop import _MAX_TOOL_RESULT_CHARS, _truncate_tool_result
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateTool

    tool = await _delegate_tool(stores, tmp_path, "delegation_longpartial@sbot.ai")

    async def long_partial(*_args, **_kwargs):
        return SpecialistOutcome(text="ก" * (_MAX_TOOL_RESULT_CHARS + 5000), timed_out=True)

    tool.runner.run = long_partial

    result = await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย")
    assert len(result) > _MAX_TOOL_RESULT_CHARS

    on_the_wire = _truncate_tool_result(result, _MAX_TOOL_RESULT_CHARS)
    assert "อย่าลงมือทำงานเดิมซ้ำเอง" in on_the_wire


@pytest.mark.asyncio
async def test_a_blank_specialist_reply_is_replaced_rather_than_stored(stores, tmp_path):
    """A reply of only newlines is truthy, so it used to pass the `or` guard on
    the success path and become the specialist's stored message — a bubble with
    a name and a role and nothing in it, on reload as well as live."""
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateTool

    tool = await _delegate_tool(stores, tmp_path, "delegation_blank@sbot.ai")

    async def blank(*_args, **_kwargs):
        return SpecialistOutcome(text="\n\n  \n")

    tool.runner.run = blank

    seen: list[dict] = []
    await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)

    assert seen[1]["text"].strip() == seen[1]["text"]
    assert seen[1]["text"]
    # Nothing stopped it, so there is no limit to name and nothing for the user
    # to make smaller — this must not claim it ran out of time.
    assert seen[1]["is_error"] is True
    assert seen[1]["result"]["failure_reason"] == "empty_output"


@pytest.mark.asyncio
async def test_the_stand_in_reply_is_written_in_the_users_language(stores, tmp_path):
    """The stand-in is not a note to the model: it is stored as the specialist's
    own message and rendered in the transcript, so a hardcoded language shows
    the wrong one to everyone else and is baked into the history permanently."""
    from sbot.core.specialist import SpecialistOutcome
    from sbot.core.turn_context import current_turn_locale
    from sbot.tools.cos import DelegateTool

    tool = await _delegate_tool(stores, tmp_path, "delegation_locale@sbot.ai")

    async def cut_off(*_args, **_kwargs):
        return SpecialistOutcome(text="", timed_out=True)

    tool.runner.run = cut_off

    async def spoken(locale: str | None) -> str:
        token = current_turn_locale.set(locale)
        try:
            seen: list[dict] = []
            await tool.execute(task="หาข้อมูล", bot_name="นักวิจัย", progress=seen.append)
            return seen[1]["text"]
        finally:
            current_turn_locale.reset(token)

    assert "ran out of time" in await spoken("en")
    assert "หมดเวลา" in await spoken("th")
    # No turn behind it (a mission node) falls back rather than failing.
    assert "ran out of time" in await spoken(None)


@pytest.mark.asyncio
async def test_a_specialist_is_told_how_much_time_it_has(stores, tmp_path):
    """The budget is enforced only by a hard cut-off, so a specialist that is
    never told the number plans as if time were free — one run spent all of it
    on two dozen searches and a large file write and returned nothing."""
    from sbot.core.specialist import SpecialistRunner

    captured: dict = {}

    class FakeLoop:
        def __init__(self, **kwargs):
            captured["max_turn_seconds"] = kwargs["max_turn_seconds"]

        async def run_turn(self, _turn_id, messages, *_args, **_kwargs):
            captured["system"] = messages[0]["content"]

            class Outcome:
                final_content = "done"
                usage = {}
                artifacts = []
                timed_out = False
                reached_max_iterations = False

            return Outcome()

    runner = SpecialistRunner(provider=None, sandbox=None, workspace=tmp_path)

    import sbot.core.specialist as specialist_module

    original = specialist_module.AgentLoop
    specialist_module.AgentLoop = FakeLoop
    try:
        class Bot:
            id = "b1"
            kind = "specialist"
            tool_allowlist = ["read_file"]
            model = None

        await runner.run(
            Bot(), "You are a researcher.", "หาข้อมูล", lambda _e: None, "t1", max_iterations=8
        )
    finally:
        specialist_module.AgentLoop = original

    assert "You are a researcher." in captured["system"]
    # The number the loop will actually enforce, not the nominal default.
    assert f"{int(captured['max_turn_seconds'])} seconds" in captured["system"]
    # `max_iterations` counts provider round-trips and the round that delivers
    # the reply spends one, so only 7 of 8 can carry tool calls. Advertising 8
    # left a model that planned against it no round in which to answer — the
    # empty-output run this notice exists to prevent.
    assert "7 rounds of tool calls" in captured["system"]
    assert "8 model turns in total" in captured["system"]


@pytest.mark.asyncio
async def test_cos_can_curate_which_skills_a_new_bot_sees(stores, tmp_path):
    """Chief of Staff decides a specialist's charter and tools, so it has to be
    able to decide its skills too — otherwise every bot it creates sees every
    skill the owner has, and a narrow specialist's prompt fills up with
    instructions for work it was never meant to do."""
    from sbot.tools.cos import CreateBotTool

    user = await stores["users"].get_or_create_by_email("curate@sbot.ai")
    tool = CreateBotTool(stores["bots"], user.id, creator_bot_id="cos")

    await tool.execute(
        name="นักวิเคราะห์ตลาด",
        role_title="Market Analyst",
        charter="วิเคราะห์ตลาด",
        skill_ids=["market-research", "  ", "competitor-scan"],
    )

    bot = await stores["bots"].get_by_name(user.id, "นักวิเคราะห์ตลาด")
    # Blanks dropped: an empty name would narrow the bot for no reason.
    assert bot.skill_ids == ["market-research", "competitor-scan"]


@pytest.mark.asyncio
async def test_cos_cannot_choose_a_new_bots_model(stores, tmp_path):
    """A bot's model is resolved on the specialist path without the caller's
    plan cost ceiling, so a model-chosen model would be a way to reach a pricier
    tier than the user pays for. Setting it stays with the user."""
    from sbot.tools.cos import CreateBotTool

    user = await stores["users"].get_or_create_by_email("nomodel@sbot.ai")
    tool = CreateBotTool(stores["bots"], user.id, creator_bot_id="cos")

    assert "model" not in tool.parameters["properties"]
    await tool.execute(
        name="ผู้ช่วย",
        role_title="Assistant",
        charter="ช่วยงาน",
        model="an-expensive-model",
    )

    bot = await stores["bots"].get_by_name(user.id, "ผู้ช่วย")
    assert bot.model is None


@pytest.mark.asyncio
async def test_delegation_recovery_is_bounded_and_preserves_cost(stores, tmp_path):
    from sbot.core.specialist import SpecialistOutcome
    tool = await _delegate_tool(stores, tmp_path, 'recovery@test.local')
    tool.reliability.retry_empty_output = True
    calls = []

    async def run(*args, **kwargs):
        calls.append(kwargs)
        return SpecialistOutcome(text='' if len(calls) == 1 else 'Recovered result', cost={'tokens': 11})

    tool.runner.run = run
    seen = []
    await tool.execute(task='Research', bot_name='นักวิจัย', progress=seen.append)
    final = next(e for e in seen if e.get('kind') == 'delegation_finished')
    assert len(calls) == 2
    assert calls[1]['max_iterations'] == 2
    assert calls[1]['max_seconds'] <= 30
    assert final['result']['status'] == 'completed'
    assert final['result']['cost']['tokens'] == 22


@pytest.mark.asyncio
async def test_batch_reports_missing_assignment_by_id(stores, tmp_path):
    from sbot.core.specialist import SpecialistOutcome
    from sbot.tools.cos import DelegateManyTool
    tool = await _delegate_tool(stores, tmp_path, 'batch_contract@test.local')

    async def run(bot, system, task, emit, **kwargs):
        if task == 'one':
            await asyncio.sleep(0.01)
        return SpecialistOutcome(text='first result' if task == 'one' else '')

    tool.runner.run = run
    result = await DelegateManyTool(tool).execute(assignments=[
        {'id': 'first', 'task': 'one', 'bot_name': 'นักวิจัย'},
        {'id': 'second', 'task': 'two', 'bot_name': 'นักวิจัย'},
    ])
    counts = json.loads(result.split('\n\n---\n\n')[0])
    assert counts == {'expected': 2, 'completed': 1, 'incomplete_ids': ['second']}
    assert '[Assignment first]' in result


@pytest.mark.asyncio
async def test_declared_file_requires_recorded_completion(stores, tmp_path):
    from sbot.core.specialist import SpecialistOutcome
    tool = await _delegate_tool(stores, tmp_path, 'required_file@test.local')

    async def run(*args, **kwargs):
        assert kwargs['extra_tools'][0].name == 'finish_step'
        assert kwargs['extra_tools'][0].require_record is True
        return SpecialistOutcome(text='I created the file!')

    tool.runner.run = run
    seen = []
    await tool.execute(task='Write a report', bot_name='นักวิจัย', required_files=['report.docx'], progress=seen.append)
    final = next(e for e in seen if e['kind'] == 'delegation_finished')
    assert final['is_error']
    assert final['result']['failure_reason'] == 'missing_completion_record'


@pytest.mark.asyncio
async def test_declared_file_does_not_restart_full_task_after_empty_output(stores, tmp_path):
    """Artifact work gets one finish-only recovery inside AgentLoop.  Delegate
    must not also launch its older whole-task recovery, which could repeat file
    writes or external actions after useful work already happened."""
    from sbot.core.specialist import SpecialistOutcome
    tool = await _delegate_tool(stores, tmp_path, 'no-replay@test.local')
    tool.reliability.retry_empty_output = True
    calls = []

    async def run(*args, **kwargs):
        calls.append(kwargs)
        completion = kwargs['extra_tools'][0]
        assert completion.require_record is True
        return SpecialistOutcome(text='')

    tool.runner.run = run
    seen = []
    await tool.execute(
        task='Build the TOR document', bot_name='นักวิจัย',
        required_files=['tor.docx'], progress=seen.append,
    )

    final = next(e for e in seen if e['kind'] == 'delegation_finished')
    assert len(calls) == 1
    assert final['is_error']
    assert final['result']['attempts'] == 1
    assert final['result']['failure_reason'] == 'missing_completion_record'


@pytest.mark.asyncio
async def test_declared_file_empty_reply_gets_finish_only_recovery(stores, tmp_path):
    """The synchronous compatibility path must use the same safe completion
    recovery as background jobs: keep history, offer only finish_step, and
    record a truthful terminal status rather than replaying the assignment."""
    provider = FakeProvider([
        [ChatResult(content='')],
        tool_call_turn('finish_step', {
            'status': 'failed',
            'summary': 'The document could not be completed.',
            'evidence': 'No valid output file was produced.',
            'files': [],
        }, call_id='finish'),
    ])
    tool = await _delegate_tool(stores, tmp_path, 'finish-only@test.local')
    tool.runner.provider = provider

    seen = []
    await tool.execute(
        task='Build the TOR document', bot_name='นักวิจัย',
        required_files=['tor.docx'], progress=seen.append,
    )

    final = next(e for e in seen if e.get('kind') == 'delegation_finished')
    assert len(provider.calls) == 2
    assert provider.offered_tools[-1] == ['finish_step']
    assert final['is_error']
    assert final['result']['status'] == 'failed'
    assert final['result']['failure_reason'] == 'failed'
