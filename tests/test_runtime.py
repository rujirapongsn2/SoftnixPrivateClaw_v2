"""End-to-end runtime test: message in → events on bus → messages in DB."""

import asyncio
import json

import pytest

from claw.config import SandboxSettings, Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime, TurnFailed
from claw.db.stores import LLMConfigStore
from claw.providers.base import ProviderError
from tests.conftest import FakeProvider, text_turn


def make_runtime(stores, provider, tmp_path, policy=None, llm_config=None) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
    )
    memory = MemoryService(
        stores["memories"], stores["messages"], stores["sessions"], provider
    )
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

    result = await runtime.handle_message(
        user.id, session.id, "what is this?", media=[str(image_path)]
    )

    assert "vision-capable model" in result.lower()
