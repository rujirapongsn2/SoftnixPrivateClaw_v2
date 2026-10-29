"""PrivateClaw runtime: which failures the agent explains and which get a system fallback."""

import asyncio
import re
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from claw.core.runtime import TurnFailed
from claw.i18n import t
from claw.jobs.store import LeaseLost
from claw.providers.base import ChatResult, ProviderError, ToolCall
from claw.tools.base import Tool
from tests.conftest import FakeProvider, text_turn
from tests.test_runtime import make_runtime


class Lookup(Tool):
    name = "mcp_linear_list_issues"
    description = "List issues"
    parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

    def __init__(self, *, broken_validator: bool = False, raises: BaseException | None = None):
        self.calls = 0
        self.raises = raises
        if broken_validator:
            self.validate_params = self.check_params = self._crash

    @staticmethod
    def _crash(_params):
        raise TypeError("unhashable type: 'list'")

    async def execute(self, **_: Any) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return "3 open issues"


def tool_call(id_: str = "c1") -> list:
    return [ChatResult(content=None, tool_calls=[ToolCall(id=id_, name=Lookup.name, arguments={"q": "open"})])]


async def turn(stores, provider, tmp_path, tool: Lookup, email: str):
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email(email)
    session = await stores["sessions"].create(user.id)
    runtime.get_agent(user.id).tools.register(tool)
    events: list[dict] = []

    async def listen():
        async with runtime.bus.subscribe(session.id) as queue:
            while True:
                event = (await queue.get()).to_dict()
                events.append(event)
                if event["type"] == "turn_error":
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    return runtime, user, session, events, listener


async def test_incident_validator_crash_is_explained_by_the_agent(stores, tmp_path):
    tool = Lookup(broken_validator=True)
    provider = FakeProvider([tool_call(), text_turn("I couldn't read your Linear issues; the tool is broken.")])
    runtime, user, session, events, listener = await turn(stores, provider, tmp_path, tool, "inc@x.y")

    final = await runtime.handle_message(user.id, session.id, "list my issues")
    listener.cancel()

    assert final == "I couldn't read your Linear issues; the tool is broken."
    assert tool.calls == 0
    assert not [e for e in events if e["type"] == "turn_error"]
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert "[tool_definition_problem;" in history[2]["content"]


async def test_tool_error_then_provider_failure_is_a_system_notice_citing_the_tool_error(stores, tmp_path):
    class DiesSecond(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            if self.calls:
                self.calls.append(list(messages))
                raise ProviderError("connection refused")
            async for event in super().stream_chat(messages, **kwargs):
                yield event

    provider = DiesSecond([tool_call()])
    runtime, user, session, events, listener = await turn(
        stores, provider, tmp_path, Lookup(broken_validator=True), "explain-dies@x.y"
    )

    message = await runtime.handle_message(user.id, session.id, "list my issues")
    await asyncio.wait_for(listener, 2)

    tool_text = next(m for m in provider.calls[1] if m.get("role") == "tool")["content"]
    error_id = re.search(r"error_id=(te_[0-9a-f]{10})", tool_text).group(1)
    assert message == t(
        "error.tool_unexplained", "en", tool=Lookup.name, error_id=error_id, reason=t("reason.network", "en")
    )
    assert (events[-1]["type"], events[-1]["message"]) == ("turn_error", message)
    started = [e["call_id"] for e in events if e["type"] == "tool_started"]
    finished = [e["call_id"] for e in events if e["type"] == "tool_finished"]
    assert started == finished == ["c1"]
    # The notice is not agent text: nothing but the user's message is stored.
    assert [m["role"] for m in await stores["messages"].recent(session.id)] == ["user"]


class Mailer(Tool):
    name = "mcp_mail_send"
    description = "Send an email"
    parameters = {"type": "object", "properties": {"to": {"type": "string"}}}

    async def execute(self, **_: Any) -> str:
        return "sent"


async def test_the_notice_names_calls_in_the_same_step_that_did_complete(stores, tmp_path):
    class DiesSecond(FakeProvider):
        async def stream_chat(self, messages, **kwargs):
            if self.calls:
                self.calls.append(list(messages))
                raise ProviderError("connection refused")
            async for event in super().stream_chat(messages, **kwargs):
                yield event

    provider = DiesSecond([[ChatResult(content=None, tool_calls=[
        ToolCall(id="c1", name=Lookup.name, arguments={"q": "open"}),
        ToolCall(id="c2", name=Mailer.name, arguments={"to": "a@example.invalid"}),
    ])]])
    runtime, user, session, events, listener = await turn(
        stores, provider, tmp_path, Lookup(raises=RuntimeError("boom")), "batch@x.y"
    )
    runtime.get_agent(user.id).tools.register(Mailer())

    message = await runtime.handle_message(user.id, session.id, "list and mail")
    await asyncio.wait_for(listener, 2)

    tool_text = next(m for m in provider.calls[1] if m.get("tool_call_id") == "c1")["content"]
    error_id = re.search(r"error_id=(te_[0-9a-f]{10})", tool_text).group(1)
    assert f"The tool {Lookup.name} failed (error ID {error_id})" in message
    assert message.endswith(t("error.tool_unexplained_completed", "en", tools=Mailer.name))
    # The mail did go out, so the notice must not say nothing ran.
    assert "Nothing ran" not in message


async def test_direct_provider_failure_keeps_its_classification(stores, tmp_path):
    class Down(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("connection refused")
            yield  # pragma: no cover

    runtime, user, session, _, listener = await turn(stores, Down([]), tmp_path, Lookup(), "down@x.y")
    message = await runtime.handle_message(user.id, session.id, "hi")
    await asyncio.wait_for(listener, 2)

    assert message == t("error.llm", "en", reason=t("reason.network", "en"))


async def test_internal_failure_is_not_reported_as_the_model(stores, tmp_path):
    class Buggy(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise AttributeError("'NoneType' object has no attribute 'sk-test-SYNTHETIC'")
            yield  # pragma: no cover

    runtime, user, session, events, listener = await turn(stores, Buggy([]), tmp_path, Lookup(), "bug@x.y")
    with pytest.raises(TurnFailed) as failure:
        await runtime.handle_message(user.id, session.id, "hi")
    await asyncio.wait_for(listener, 2)

    message = failure.value.message
    assert re.fullmatch(
        re.escape(t("error.runtime", "en", error_id="ERRID")).replace("ERRID", r"te_[0-9a-f]{10}"), message
    )
    assert "could not be reached" not in message and "sk-test" not in message
    assert t("error.runtime", "th", error_id="x") != t("error.runtime", "en", error_id="x")


async def test_lease_loss_from_a_tool_propagates_out_of_the_turn(stores, tmp_path):
    provider = FakeProvider([tool_call(), text_turn("must not be reached")])
    runtime, user, session, _, listener = await turn(
        stores, provider, tmp_path, Lookup(raises=LeaseLost("moved")), "lease@x.y"
    )

    with pytest.raises(LeaseLost):
        await runtime.handle_message(user.id, session.id, "hi")
    listener.cancel()
    assert len(provider.calls) == 1


async def test_save_failure_after_a_completed_action_reports_only_the_save(stores, tmp_path):
    tool = Lookup()
    provider = FakeProvider([tool_call(), text_turn("There are 3 open issues.")])
    runtime, user, session, events, listener = await turn(stores, provider, tmp_path, tool, "save@x.y")
    real_append = stores["messages"].append
    seen = {"n": 0}

    async def fail_answer_save(*args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return await real_append(*args, **kwargs)
        raise SQLAlchemyError("disk full")

    stores["messages"].append = fail_answer_save
    with pytest.raises(TurnFailed) as failure:
        await runtime.handle_message(user.id, session.id, "how many?")
    await asyncio.wait_for(listener, 2)

    assert tool.calls == 1 and len(provider.calls) == 2  # nothing re-ran
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["turn_completed", "turn_error"]
    assert events[-2]["content"] == "There are 3 open issues."
    assert failure.value.message == t("error.save", "en")
    assert "try again" not in failure.value.message.lower()
