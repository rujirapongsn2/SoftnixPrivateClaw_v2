"""End-to-end runtime test: message in → events on bus → messages in DB."""

import asyncio
import json

import pytest

from sqlalchemy.exc import SQLAlchemyError

from claw.config import LLMSettings, SandboxSettings, Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime, TurnFailed
from claw.db.stores import LLMConfigStore, UsageStore
from claw.i18n import t
from claw.providers.base import ChatResult, ProviderError, TextDelta, ToolCall
from tests.conftest import FakeProvider, text_turn


def make_runtime(
    stores, provider, tmp_path, policy=None, llm_config=None, llm=None, usage=None
) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=llm or LLMSettings(),
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings,
        provider=provider,
        bus=EventBus(),
        users=stores["users"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=memory,
        audit=stores["audit"],
        policy=policy,
        llm_config=llm_config,
        usage=usage,
    )


async def test_handle_message_streams_and_persists(stores, tmp_path):
    provider = FakeProvider([text_turn("สวัสดีครับ ผมช่วยอะไรได้บ้าง")])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("u@x.y")
    session = await stores["sessions"].create(user.id)

    received = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = await queue.get()
                received.append(event.to_dict())
                if event.to_dict()["type"] in ("turn_completed", "turn_error"):
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)  # let the subscriber attach before the turn starts
    final = await runtime.handle_message(user.id, session.id, "สวัสดี")
    await asyncio.wait_for(listener, 2)

    assert final == "สวัสดีครับ ผมช่วยอะไรได้บ้าง"
    types = [e["type"] for e in received]
    assert types[0] == "turn_started"
    assert "text_delta" in types
    assert types[-1] == "turn_completed"

    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    # Stored user message must NOT include the runtime-context header.
    assert history[0]["content"] == "สวัสดี"


async def test_provider_error_does_not_poison_history(stores, tmp_path):
    class ExplodingProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("429 rate limit")
            yield  # pragma: no cover

    runtime = make_runtime(stores, ExplodingProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("u@x.y")
    session = await stores["sessions"].create(user.id)

    result = await runtime.handle_message(user.id, session.id, "hello")

    assert result  # localized error message returned
    history = await stores["messages"].recent(session.id)
    # Only the user message is persisted — no assistant error text.
    assert [m["role"] for m in history] == ["user"]


async def test_provider_unavailable_gets_specific_user_message(stores, tmp_path):
    class UnavailableProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError(
                "h2 protocol error: error reading a body from connection",
                error_type="provider_unavailable",
                retryable=False,
            )
            yield  # pragma: no cover

    runtime = make_runtime(stores, UnavailableProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("unavailable@x.y")
    session = await stores["sessions"].create(user.id)

    result = await runtime.handle_message(user.id, session.id, "hello")

    assert result == t("error.provider_unavailable", "en")
    history = await stores["messages"].recent(session.id)
    assert [message["role"] for message in history] == ["user"]


async def test_local_dns_failure_remains_a_network_error(stores, tmp_path):
    class DnsFailureProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("DNS lookup failed for local gateway", retryable=False)
            yield  # pragma: no cover

    runtime = make_runtime(stores, DnsFailureProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("dns@x.y")
    session = await stores["sessions"].create(user.id)

    result = await runtime.handle_message(user.id, session.id, "hello")

    assert t("reason.network", "en") in result
    assert t("error.provider_unavailable", "en") != result


async def test_provider_failure_after_text_keeps_and_marks_partial_answer(stores, tmp_path):
    class PartialProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            yield TextDelta(text="Completed the safe first part")
            raise ProviderError(
                "upstream provider unavailable",
                error_type="provider_unavailable",
                retryable=True,
            )

    runtime = make_runtime(stores, PartialProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("partial-provider@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "do work")

    assert final.startswith("Completed the safe first part")
    assert t("error.provider_stream_partial", "en") in final
    history = await stores["messages"].recent(session.id)
    assert [message for message in history if message["role"] == "assistant"][-1]["content"] == final


async def test_unhandled_exception_raises_turn_failed(stores, tmp_path):
    """A non-ProviderError exception must raise TurnFailed (not return a
    plain string) so callers that branch on success/failure by return value
    (SchedulerService, HeartbeatService) see a real failure — a truthy
    "friendly error" string previously made a failed scheduled run look ok."""

    class BoomProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    runtime = make_runtime(stores, BoomProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("boom@x.y")
    session = await stores["sessions"].create(user.id)

    received = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = await queue.get()
                received.append(event.to_dict())
                if event.to_dict()["type"] in ("turn_completed", "turn_error"):
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    with pytest.raises(TurnFailed):
        await runtime.handle_message(user.id, session.id, "hello")
    await asyncio.wait_for(listener, 2)

    assert received[-1]["type"] == "turn_error"
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user"]


async def test_sqlalchemy_error_outside_the_transcript_write_is_not_misattributed(stores, tmp_path):
    """A SQLAlchemyError raised somewhere other than the transcript save (e.g.
    a tool touching the DB) must not surface as "your message wasn't saved" —
    only the messages.append call sites are tagged as save failures."""

    class DbBoomProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise SQLAlchemyError("boom")
            yield  # pragma: no cover

    runtime = make_runtime(stores, DbBoomProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("dbboom@x.y")
    session = await stores["sessions"].create(user.id)

    received = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = await queue.get()
                received.append(event.to_dict())
                if event.to_dict()["type"] in ("turn_completed", "turn_error"):
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    with pytest.raises(TurnFailed):
        await runtime.handle_message(user.id, session.id, "hello")
    await asyncio.wait_for(listener, 2)

    assert received[-1]["type"] == "turn_error"
    assert received[-1]["message"] == t("error.llm", "en", reason=t("reason.internal", "en"))


async def test_sqlalchemy_error_from_the_transcript_write_still_says_save_failed(stores, tmp_path):
    """The post-generation transcript write failing (the case error.save
    exists for) must still surface that specific message, not the generic
    one — and not error.save_request, which is for the earlier, pre-generation
    save point (see the test below)."""

    runtime = make_runtime(stores, FakeProvider([text_turn("hi")]), tmp_path)
    user = await stores["users"].get_or_create_by_email("saveboom@x.y")
    session = await stores["sessions"].create(user.id)

    real_append = stores["messages"].append
    calls = {"n": 0}

    async def boom_after_first(*args, **kwargs):
        # First call is the pre-turn user-message save; let it succeed so this
        # test exercises the later, post-generation save instead.
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_append(*args, **kwargs)
        raise SQLAlchemyError("boom")

    stores["messages"].append = boom_after_first

    received = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = await queue.get()
                received.append(event.to_dict())
                if event.to_dict()["type"] in ("turn_completed", "turn_error"):
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    with pytest.raises(TurnFailed):
        await runtime.handle_message(user.id, session.id, "hello")
    await asyncio.wait_for(listener, 2)

    assert received[-1]["type"] == "turn_error"
    assert received[-1]["message"] == t("error.save", "en")


async def test_sqlalchemy_error_saving_the_user_message_says_request_not_answer(stores, tmp_path):
    """Failing to persist the user's own message — before the model is ever
    called — must not claim an answer was generated (that would be false)."""

    runtime = make_runtime(stores, FakeProvider([text_turn("hi")]), tmp_path)
    user = await stores["users"].get_or_create_by_email("saveboom2@x.y")
    session = await stores["sessions"].create(user.id)

    async def boom_append(*args, **kwargs):
        raise SQLAlchemyError("boom")

    stores["messages"].append = boom_append

    received = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = await queue.get()
                received.append(event.to_dict())
                if event.to_dict()["type"] in ("turn_completed", "turn_error"):
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    with pytest.raises(TurnFailed):
        await runtime.handle_message(user.id, session.id, "hello")
    await asyncio.wait_for(listener, 2)

    assert received[-1]["type"] == "turn_error"
    assert received[-1]["message"] == t("error.save_request", "en")


async def test_truncated_empty_answer_is_reported_not_silently_blank(stores, tmp_path):
    """A reasoning model that burns its whole output cap on hidden thinking
    returns finish_reason="length" with no content. That must surface as a
    visible "cut off" message — an empty turn_completed looks to the user like
    the app finished and did nothing."""

    provider = FakeProvider([[ChatResult(content=None, finish_reason="length")]])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("cut@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "สร้างไฟล์ pdf")

    assert final == t("error.truncated", "th")
    # The model's own blank turn must not reach storage as-is (it would render
    # as an empty bubble and come back as content-less history), but the
    # fallback explanation the user was actually shown must — otherwise a
    # reload shows the user's message with no reply at all.
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == t("error.truncated", "th")


async def test_empty_answer_without_truncation_gets_its_own_message(stores, tmp_path):
    provider = FakeProvider([[ChatResult(content=None, finish_reason="stop")]])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("blank@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "hello")

    assert final == t("error.empty_response", "en")
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == t("error.empty_response", "en")


async def test_files_created_by_a_turn_that_ends_empty_are_still_reachable(stores, tmp_path):
    # The model writes a file and then hits its output cap before writing any
    # closing text. Without an assistant message to carry them, the artifacts
    # would be dropped from the transcript and the file unreachable on reload.
    write = ToolCall(id="w1", name="write_file", arguments={"path": "report.txt", "content": "hi"})
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[write])],
            [ChatResult(content=None, finish_reason="length")],
        ]
    )
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("art@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "write a report")

    # The "no answer" explanation must also NAME the work that did get done —
    # otherwise the turn reads as a total failure while the file sits unmentioned.
    assert final.startswith(t("error.truncated", "en"))
    assert "report.txt" in final
    history = await stores["messages"].recent(session.id)
    carriers = [m for m in history if (m.get("meta") or {}).get("artifacts")]
    assert [m["role"] for m in carriers] == ["assistant"]
    assert carriers[0]["meta"]["artifacts"] == ["report.txt"]
    # The message the user saw is the one that got persisted.
    assert carriers[0]["content"] == final


class SlowProvider(FakeProvider):
    async def stream_chat(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        async for event in super().stream_chat(*args, **kwargs):
            yield event


async def test_turn_that_runs_out_of_time_says_so_rather_than_blaming_the_step_limit(stores, tmp_path):
    call = ToolCall(id="c1", name="read_file", arguments={"path": "missing.txt"})
    provider = SlowProvider([[ChatResult(content=None, tool_calls=[call])] for _ in range(5)])
    runtime = make_runtime(stores, provider, tmp_path, llm=LLMSettings(max_turn_seconds=0.01))
    user = await stores["users"].get_or_create_by_email("slow@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "do something slow")

    assert final == t("error.turn_timeout", "en")


class HalfAnswerProvider(FakeProvider):
    """Streams part of an answer, then stalls until the turn's deadline fires."""

    async def stream_chat(self, *args, **kwargs):
        yield TextDelta(text="Step one is done")
        await asyncio.sleep(60)
        yield ChatResult(content="unreachable")


async def test_answer_cut_off_by_the_deadline_is_kept_but_marked_as_unfinished(stores, tmp_path):
    runtime = make_runtime(stores, HalfAnswerProvider([]), tmp_path, llm=LLMSettings(max_turn_seconds=0.05))
    user = await stores["users"].get_or_create_by_email("cut@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "do a long job")

    # The partial text the user already watched stream in is kept...
    assert final.startswith("Step one is done")
    # ...but must be marked, or it reads as a complete answer that trails off.
    assert t("error.turn_timeout_partial", "en") in final
    history = await stores["messages"].recent(session.id)
    assistant = [m for m in history if m["role"] == "assistant"]
    # What was shown is what is stored — a reload must not lose the marker, and
    # the next turn must not get the fragment back as if it were the whole reply.
    assert assistant[-1]["content"] == final


async def test_policy_masked_output_is_persisted_masked(stores, tmp_path):
    from claw.security.policy import PolicyEngine

    provider = FakeProvider([text_turn("you can reach him at leak@corp.com")])
    runtime = make_runtime(stores, provider, tmp_path, policy=PolicyEngine())
    user = await stores["users"].get_or_create_by_email("mask@x.y")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "who do I contact")

    assert "leak@corp.com" not in final
    history = await stores["messages"].recent(session.id)
    # The transcript is re-served on reload and replayed to the model, so storing
    # the raw address would hand back exactly what the rule just removed.
    assert "leak@corp.com" not in json.dumps(history)
    assert [m for m in history if m["role"] == "assistant"][-1]["content"] == final


async def test_turns_serialize_per_session_not_globally(stores, tmp_path):
    provider = FakeProvider([text_turn("a"), text_turn("b")])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("u@x.y")
    s1 = await stores["sessions"].create(user.id)
    s2 = await stores["sessions"].create(user.id)

    r1, r2 = await asyncio.gather(
        runtime.handle_message(user.id, s1.id, "one"),
        runtime.handle_message(user.id, s2.id, "two"),
    )
    assert {r1, r2} == {"a", "b"}


async def test_policy_masks_input_before_storage(stores, tmp_path):
    from claw.security.policy import PolicyEngine

    provider = FakeProvider([text_turn("noted")])
    runtime = make_runtime(stores, provider, tmp_path, policy=PolicyEngine())
    user = await stores["users"].get_or_create_by_email("p@x.y")
    session = await stores["sessions"].create(user.id)

    await runtime.handle_message(user.id, session.id, "my email is secret@corp.com")

    history = await stores["messages"].recent(session.id)
    stored_user = next(m for m in history if m["role"] == "user")
    # Raw PII must never be persisted.
    assert "secret@corp.com" not in stored_user["content"]
    assert "[REDACTED_EMAIL]" in stored_user["content"]
    # The model saw the masked text, not the raw address.
    assert all("secret@corp.com" not in json.dumps(call) for call in provider.calls)


async def test_policy_blocks_input(stores, tmp_path):
    from claw.security.policy import Action, PolicyEngine, PolicyRule

    engine = PolicyEngine(rules=[PolicyRule("nope", r"forbidden", Action.BLOCK, block_message="Denied.")])
    provider = FakeProvider([text_turn("should not run")])
    runtime = make_runtime(stores, provider, tmp_path, policy=engine)
    user = await stores["users"].get_or_create_by_email("p2@x.y")
    session = await stores["sessions"].create(user.id)

    result = await runtime.handle_message(user.id, session.id, "do the forbidden thing")

    assert result == "Denied."
    assert provider.calls == []  # model never invoked
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user"]


@pytest.mark.parametrize("locale,expected_fragment", [("th", "โมเดล"), ("en", "AI model")])
async def test_error_messages_are_localized(stores, tmp_path, locale, expected_fragment):
    class ExplodingProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("connection timeout")
            yield  # pragma: no cover

    runtime = make_runtime(stores, ExplodingProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email(f"u-{locale}@x.y")
    session = await stores["sessions"].create(user.id)

    result = await runtime.handle_message(user.id, session.id, "hi", locale=locale)
    assert expected_fragment in result


async def test_image_attachment_short_circuits_for_text_only_model(stores, db_factory, tmp_path):
    """deepseek-chat is registered as text-only in the provider registry — an
    image attachment must be rejected before ever calling the provider, not
    after a wasted (and confusingly-generic) round trip."""
    llm_config = LLMConfigStore(db_factory)
    provider_row = await llm_config.create_provider("prov-ds", "sk-test", "", True, "", owner_id=None)
    await llm_config.create_model(
        provider_row.id, "deepseek-chat", "DeepSeek Chat", True, "medium", "", kind="chat", owner_id=None
    )

    provider = FakeProvider([text_turn("should not run")])
    runtime = make_runtime(stores, provider, tmp_path, llm_config=llm_config)
    user = await stores["users"].get_or_create_by_email("v@x.y")
    session = await stores["sessions"].create(user.id)

    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    result = await runtime.handle_message(
        user.id, session.id, "what is this?", media=[str(image_path)], model="deepseek-chat"
    )

    assert "vision-capable model" in result.lower()
    assert provider.calls == []  # model never invoked

    # The user's message + attachment note must still land in history (same as
    # the policy-blocked path) even though the turn was rejected pre-provider.
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user"]
    assert "photo.png" in history[0]["content"]


class _RecordingProvider(FakeProvider):
    """FakeProvider that also remembers which model each call was routed to."""

    def __init__(self, turns):
        super().__init__(turns)
        self.models: list[str | None] = []

    async def stream_chat(self, messages, tools=None, model=None, *args, **kwargs):
        self.models.append(model)
        async for event in super().stream_chat(messages, tools, model, *args, **kwargs):
            yield event


async def _with_vision_model(db_factory):
    llm_config = LLMConfigStore(db_factory)
    prov = await llm_config.create_provider("prov-ds", "sk-test", "", True, "", owner_id=None)
    await llm_config.create_model(
        prov.id, "deepseek-chat", "DeepSeek Chat", True, "medium", "", kind="chat", owner_id=None
    )
    await llm_config.create_model(
        prov.id, "vendor/eyes-1", "Eyes", True, "low", "", kind="vision", owner_id=None
    )
    return llm_config


async def test_image_attachment_is_delegated_to_the_vision_model(stores, db_factory, tmp_path):
    """With a kind="vision" model configured, a text-only chat model no longer
    refuses an image: the vision model reads it and the chat model answers from
    the description. Both upstream calls are billed, and both count as turns."""
    llm_config = await _with_vision_model(db_factory)
    usage = UsageStore(db_factory, is_postgres=False)
    provider = _RecordingProvider(
        [
            [ChatResult(content="A bar chart. The tallest bar is labelled 42 errors.",
                        usage={"prompt_tokens": 30, "completion_tokens": 12})],
            text_turn("Your chart shows 42 errors."),
        ]
    )
    runtime = make_runtime(stores, provider, tmp_path, llm_config=llm_config, usage=usage)
    user = await stores["users"].get_or_create_by_email("vd@x.y")
    session = await stores["sessions"].create(user.id)
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    result = await runtime.handle_message(
        user.id, session.id, "how many errors?", media=[str(image_path)], model="deepseek-chat"
    )

    assert result == "Your chart shows 42 errors."
    assert provider.models == ["vendor/eyes-1", "deepseek-chat"]
    # The chat model got the description as text and no image block at all —
    # sending one is exactly what it can't handle.
    chat_call = json.dumps(provider.calls[1])
    assert "42 errors" in chat_call
    assert "image_url" not in chat_call
    # ...and the sentence pointing at images "attached below" is gone with them.
    assert "attached below" not in chat_call

    history = await stores["messages"].recent(session.id)
    answer = next(m for m in reversed(history) if m["role"] == "assistant")
    assert answer["meta"]["vision_model"] == "vendor/eyes-1"

    await runtime.drain()
    # The read is a real upstream call, so it counts against the daily quota
    # like any other turn — otherwise a user who keeps tripping the paths that
    # discard a description could retry paid reads all day for free.
    assert (await usage.usage_today(user.id))["turns"] == 2

    # The images are never persisted, so the description has to be folded into
    # the stored user message: without it every follow-up turn is blind to what
    # was attached while the transcript still claims it was read.
    history = await stores["messages"].recent(session.id)
    assert "42 errors" in history[0]["content"]
    assert "base64" not in history[0]["content"]


async def test_delegated_vision_description_is_guardrailed(stores, db_factory, tmp_path):
    """An image is another way to get text in front of the model. Whatever the
    vision model reads out of it must go through the same input policy a typed
    message does, and a block must say so rather than blaming the model choice."""
    from claw.security.policy import Action, PolicyEngine, PolicyRule

    llm_config = await _with_vision_model(db_factory)
    engine = PolicyEngine(rules=[PolicyRule("nope", r"forbidden", Action.BLOCK, block_message="Denied.")])
    provider = _RecordingProvider(
        [[ChatResult(content="A poster reading: forbidden material")], text_turn("should not run")]
    )
    runtime = make_runtime(stores, provider, tmp_path, policy=engine, llm_config=llm_config)
    user = await stores["users"].get_or_create_by_email("vg@x.y")
    session = await stores["sessions"].create(user.id)
    image_path = tmp_path / "poster.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    result = await runtime.handle_message(
        user.id, session.id, "what does it say?", media=[str(image_path)], model="deepseek-chat"
    )

    assert result == "Denied."
    assert provider.models == ["vendor/eyes-1"]  # the chat model never ran


async def test_vision_read_that_fails_falls_back_to_refusing_the_turn(stores, db_factory, tmp_path):
    """A failed read must not become the description — answering from an error
    string would look like an answer about the image."""
    llm_config = await _with_vision_model(db_factory)

    class FailingVision(_RecordingProvider):
        async def stream_chat(self, messages, tools=None, model=None, *args, **kwargs):
            self.models.append(model)
            raise ProviderError("upstream exploded")
            yield  # pragma: no cover

    provider = FailingVision([])
    runtime = make_runtime(stores, provider, tmp_path, llm_config=llm_config)
    user = await stores["users"].get_or_create_by_email("vf@x.y")
    session = await stores["sessions"].create(user.id)
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    result = await runtime.handle_message(
        user.id, session.id, "what is this?", media=[str(image_path)], model="deepseek-chat"
    )

    assert "vision-capable model" in result.lower()
    assert provider.models == ["vendor/eyes-1"]


async def test_vision_rejection_from_provider_gets_clear_message(stores, tmp_path):
    """Reactive safety net: even if the registry doesn't know a model is
    text-only, a provider-side vision rejection must surface the same clear
    "switch models" message instead of the generic internal-error text."""

    class VisionRejectingProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("BadRequestError: this model does not support image input")
            yield  # pragma: no cover

    runtime = make_runtime(stores, VisionRejectingProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("v2@x.y")
    session = await stores["sessions"].create(user.id)

    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    result = await runtime.handle_message(user.id, session.id, "what is this?", media=[str(image_path)])

    assert "vision-capable model" in result.lower()
