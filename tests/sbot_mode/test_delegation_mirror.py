"""A delegated specialist's work shows up in the specialist's own thread.

A delegation runs as a nested loop inside the leader's turn, and the event bus
is keyed by session — so everything the specialist did was only ever visible on
the leader's screen. Clicking that bot in the sidebar opened its thread and
found nothing, which is the one place a user looks to see what a bot is doing.
"""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.keyed_locks import KeyedLocks
from sbot.core.memory import MemoryService
from sbot.core.runtime import AgentRuntime
from sbot.core.specialist import DelegationMirror
from sbot.providers.base import ChatResult, ToolCall
from tests.sbot_mode.conftest import FakeProvider, text_turn


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.published: list[tuple[str, object]] = []

    def publish(self, session_id, event):
        self.published.append((session_id, event))
        super().publish(session_id, event)

    def types_on(self, session_id: str) -> list[str]:
        return [e.type for sid, e in self.published if sid == session_id]


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )


def delegating_turn(bot_id: str, task: str) -> list:
    return [
        ChatResult(
            content=None,
            tool_calls=[ToolCall(id="d1", name="delegate", arguments={"bot_id": bot_id, "task": task})],
        )
    ]


async def _team(stores, email: str):
    user = await stores["users"].get_or_create_by_email(email)
    cos = await stores["bots"].get_or_create_cos(user.id)
    bot = await stores["bots"].create(
        owner_id=user.id, name="Researcher", role_title="Analyst", charter="หาข้อมูล"
    )
    session = await stores["sessions"].create(user.id, title="cos", bot_id=cos.id, kind="direct")
    return user, bot, session


def make_runtime(stores, provider, bus, tmp_path) -> AgentRuntime:
    return AgentRuntime(
        settings=make_settings(tmp_path),
        provider=provider,
        bus=bus,
        users=stores["users"],
        bots=stores["bots"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=MemoryService(
            stores["memories"], stores["messages"], stores["sessions"], provider
        ),
        audit=stores["audit"],
    )


@pytest.mark.asyncio
async def test_a_delegated_run_is_visible_in_the_specialists_own_thread(stores, tmp_path):
    user, bot, session = await _team(stores, "mirror-visible@sbot.ai")
    provider = FakeProvider([
        delegating_turn(bot.id, "หาขนาดตลาด"),
        text_turn("ตลาดโต 12%"),
        text_turn("นักวิจัยรายงานว่าตลาดโต 12% ครับ"),
    ])
    bus = RecordingBus()

    await make_runtime(stores, provider, bus, tmp_path).handle_message(
        user.id, session.id, "ให้นักวิจัยหาข้อมูล"
    )

    mirrored = await stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name)
    assert mirrored.id != session.id
    page, _ = await stores["messages"].page_for_display(mirrored.id)
    assert [(m["role"], m["content"]) for m in page] == [
        ("user", "หาขนาดตลาด"),
        ("assistant", "ตลาดโต 12%"),
    ]
    # Shaped as an ordinary turn, so the chat renders it with no special case.
    types = bus.types_on(mirrored.id)
    assert types[0] == "turn_started"
    assert types[-1] == "turn_completed"
    assert "text_delta" in types


@pytest.mark.asyncio
async def test_the_specialists_thread_is_written_before_it_starts_working(stores, tmp_path):
    """A user who arrives while the bot is still working needs the instruction:
    a thread showing activity under no visible request is the wrong half."""
    user, bot, _ = await _team(stores, "mirror-order@sbot.ai")
    bus = RecordingBus()
    mirror = DelegationMirror(bus, stores["sessions"], stores["messages"], user.id)

    mirrored = await mirror.open(bot, "หาขนาดตลาด", "del_1")

    assert mirrored is not None
    page, _ = await stores["messages"].page_for_display(mirrored)
    assert [(m["role"], m["content"]) for m in page] == [("user", "หาขนาดตลาด")]
    # The instruction is sent, not just stored: a client already watching the
    # thread has nothing to refetch on, so an unsent one is invisible until the
    # user leaves the session and comes back.
    assert bus.types_on(mirrored) == ["turn_started", "delegated_task"]


@pytest.mark.asyncio
async def test_a_second_run_does_not_interleave_into_a_busy_thread(stores, tmp_path):
    """`delegate_many` can hand the same bot two assignments at once, and one
    thread can only show one conversation — the second stays on the leader's
    side rather than shuffling two runs together."""
    user, bot, _ = await _team(stores, "mirror-busy@sbot.ai")
    mirror = DelegationMirror(RecordingBus(), stores["sessions"], stores["messages"], user.id)

    first = await mirror.open(bot, "งานแรก", "del_1")
    second = await mirror.open(bot, "งานที่สอง", "del_2")

    assert first is not None
    assert second is None
    # And the thread is free again once the first run ends.
    await mirror.close(first, "del_1", "เสร็จ", is_error=False)
    assert await mirror.open(bot, "งานที่สาม", "del_3") == first


@pytest.mark.asyncio
async def test_a_failed_delegation_still_ends_the_mirrored_turn(stores, tmp_path):
    """The client sits on `busy` until the turn closes — a run that raises must
    not leave a spinner that never stops."""
    user, bot, session = await _team(stores, "mirror-error@sbot.ai")

    class ExplodingSpecialist(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            # The second call is the nested specialist turn; the leader's own
            # calls are scripted, so only the delegation dies here.
            if len(self.calls) == 1:
                self.calls.append([])
                raise RuntimeError("provider died")
            async for event in super().stream_chat(*args, **kwargs):
                yield event

    provider = ExplodingSpecialist([
        delegating_turn(bot.id, "หา"),
        text_turn("นักวิจัยล่มครับ"),
    ])
    bus = RecordingBus()
    await make_runtime(stores, provider, bus, tmp_path).handle_message(
        user.id, session.id, "ให้นักวิจัยหา"
    )

    mirrored = await stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name)
    assert bus.types_on(mirrored.id)[-1] == "turn_error"


@pytest.mark.asyncio
async def test_a_thread_that_cannot_be_written_does_not_take_the_delegation_down(
    stores, tmp_path
):
    """The mirror is a second view of work already being delivered elsewhere."""
    user, bot, _ = await _team(stores, "mirror-broken@sbot.ai")

    class Broken:
        async def thread_for_bot(self, *_a, **_k):
            raise RuntimeError("db down")

    mirror = DelegationMirror(RecordingBus(), Broken(), stores["messages"], user.id)

    assert await mirror.open(bot, "งาน", "del_1") is None


@pytest.mark.asyncio
async def test_an_unwritable_instruction_releases_the_thread_again(stores, tmp_path):
    """A claim that is never released would silence the bot's thread for the
    rest of the process, not just for the run that failed to open."""
    user, bot, _ = await _team(stores, "mirror-unwritable@sbot.ai")

    class RefusesOnce:
        def __init__(self, real):
            self.real = real
            self.attempts = 0

        async def append(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("write failed")
            return await self.real.append(*args, **kwargs)

    messages = RefusesOnce(stores["messages"])
    bus = RecordingBus()
    mirror = DelegationMirror(bus, stores["sessions"], messages, user.id)

    assert await mirror.open(bot, "งานแรก", "del_1") is None
    # No turn was opened, so nothing may be relayed into one.
    assert bus.published == []
    assert await mirror.open(bot, "งานที่สอง", "del_2") is not None


@pytest.mark.asyncio
async def test_an_unstorable_reply_still_ends_the_turn(stores, tmp_path):
    """The turn is over whether or not the reply could be written down, and a
    client left on `busy` shows a spinner that never stops."""
    user, bot, _ = await _team(stores, "mirror-unstorable@sbot.ai")

    class RefusesReplies:
        def __init__(self, real):
            self.real = real
            self.calls = 0

        async def append(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("write failed")
            return await self.real.append(*args, **kwargs)

    bus = RecordingBus()
    mirror = DelegationMirror(bus, stores["sessions"], RefusesReplies(stores["messages"]), user.id)

    mirrored = await mirror.open(bot, "งาน", "del_1")
    await mirror.close(mirrored, "del_1", "เสร็จแล้ว", is_error=False)

    assert bus.types_on(mirrored) == ["turn_started", "delegated_task", "turn_completed"]


@pytest.mark.asyncio
async def test_a_thread_busy_with_the_users_own_turn_is_not_mirrored_into(stores, tmp_path):
    """The mirror's own bookkeeping only ever knew about runs it opened itself.
    A user typing to that same bot holds the runtime's per-session turn lock,
    which the mirror used to walk straight past — two turns then interleaved on
    one thread, each ending the other's spinner and sharing one replay buffer.
    """
    user, bot, _ = await _team(stores, "mirror-direct-turn@sbot.ai")
    locks = KeyedLocks()
    mirror = DelegationMirror(
        RecordingBus(), stores["sessions"], stores["messages"], user.id, lock_for=locks.get
    )
    thread = await stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name)

    async with locks.get(thread.id):  # the user's own turn, already running
        # Tried, not waited for: a mirror that queued behind the user would
        # hold the whole delegation until they were done typing.
        assert await asyncio.wait_for(mirror.open(bot, "งาน", "del_1"), timeout=2) is None
    assert await mirror.open(bot, "งาน", "del_2") == thread.id


@pytest.mark.asyncio
async def test_a_rebuilt_mirror_still_sees_a_run_already_in_flight(stores, tmp_path):
    """The mirror belongs to a cached agent, and the cache evicts by least
    recent use — so a busy specialist's second delegation was handed a brand
    new mirror that knew nothing about the run still writing into that thread.
    """
    user, bot, _ = await _team(stores, "mirror-evicted@sbot.ai")
    locks = KeyedLocks()

    def build() -> DelegationMirror:
        return DelegationMirror(
            RecordingBus(), stores["sessions"], stores["messages"], user.id, lock_for=locks.get
        )

    before = build()
    assert await before.open(bot, "งานแรก", "del_1") is not None
    assert await build().open(bot, "งานที่สอง", "del_2") is None


@pytest.mark.asyncio
async def test_delegations_at_once_do_not_give_a_bot_two_threads(stores, tmp_path):
    """`delegate_many` can name the same bot twice, and the lookup and the
    insert are two separate awaits — so both calls found nothing and both
    created one. The sidebar resolves a bot to a single thread, so the loser's
    transcript is only reachable under "Other"."""
    user, bot, _ = await _team(stores, "mirror-two-threads@sbot.ai")

    # Whether the four actually overlap is otherwise up to how the driver's
    # threads happen to be scheduled — this passed unlocked about as often as
    # not. Stretching the gap between the lookup and the insert makes the window
    # the lock exists to close a certainty rather than a coin toss. A delay
    # rather than a barrier: under the lock the four run one after another, and
    # a barrier none of them could fill would hang instead of failing.
    real_scalar = AsyncSession.scalar

    async def scalar(self, *args, **kwargs):
        found = await real_scalar(self, *args, **kwargs)
        await asyncio.sleep(0.05)
        return found

    with patch.object(AsyncSession, "scalar", scalar):
        made = await asyncio.gather(
            *(stores["sessions"].thread_for_bot(user.id, bot.id, title=bot.name) for _ in range(4))
        )

    assert len({s.id for s in made}) == 1


@pytest.mark.asyncio
async def test_two_writers_at_once_do_not_land_on_the_same_seq(stores, tmp_path):
    """`seq` is read with a SELECT and committed on a later await, so two
    writers to one session can read the same maximum. A duplicate is
    unrecoverable: paging walks backwards on a strictly-less-than cursor, so
    the twin below a page boundary can never be asked for again."""
    _, _, session = await _team(stores, "mirror-seq@sbot.ai")

    await asyncio.gather(
        *(
            stores["messages"].append(session.id, [{"role": "user", "content": str(i)}])
            for i in range(8)
        )
    )

    page, _ = await stores["messages"].page_for_display(session.id)
    assert len(page) == 8
    assert len({m["seq"] for m in page}) == 8


@pytest.mark.asyncio
async def test_an_assignment_is_not_replayed_as_the_users_own_words(stores, tmp_path):
    """The instruction sits in the `user` role because that is the position it
    occupies in the bot's thread, but a leader wrote it. Unmarked, the bot's
    next direct turn reads it as something the human said — and consolidation
    renders the same rows, so it lands in the bot's memory that way too."""
    _, _, session = await _team(stores, "mirror-attribution@sbot.ai")
    await stores["messages"].append(
        session.id, [{"role": "user", "content": "หาขนาดตลาด", "meta": {"delegated_by": "cos1"}}]
    )

    assert (await stores["messages"].recent(session.id))[0]["content"] != "หาขนาดตลาด"
    # The transcript the user reads is untouched: it has the "assigned by"
    # caption instead, and a marker in the bubble would be a second copy of it.
    page, _ = await stores["messages"].page_for_display(session.id)
    assert page[0]["content"] == "หาขนาดตลาด"


@pytest.mark.asyncio
async def test_a_bot_keeps_one_thread_across_delegations(stores, tmp_path):
    """The sidebar resolves a bot row to its most recently updated thread, so a
    fresh session per delegation would push the last one out of reach."""
    user, bot, _ = await _team(stores, "mirror-one-thread@sbot.ai")
    mirror = DelegationMirror(RecordingBus(), stores["sessions"], stores["messages"], user.id)

    first = await mirror.open(bot, "งานแรก", "del_1")
    await mirror.close(first, "del_1", "เสร็จ", is_error=False)
    second = await mirror.open(bot, "งานที่สอง", "del_2")

    assert second == first
    assert len([s for s in await stores["sessions"].list_for_user(user.id) if s.bot_id == bot.id]) == 1
