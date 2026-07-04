from typing import Any

from claw.core.events import AgentEvent, TextDeltaEvent, ToolFinished, ToolStarted
from claw.core.loop import AgentLoop
from claw.providers.base import ChatResult, TextDelta, ToolCall
from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry
from tests.conftest import FakeProvider, text_turn


class EchoTool(Tool):
    name = "echo"
    description = "Echo the input"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, text: str, **_: Any) -> str:
        return f"echo: {text}"


def collector() -> tuple[list[AgentEvent], Any]:
    events: list[AgentEvent] = []
    return events, events.append


async def test_plain_text_turn_streams_and_completes():
    provider = FakeProvider([text_turn("hello there")])
    loop = AgentLoop(provider, ToolRegistry())
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "hi"}], emit)

    assert outcome.final_content == "hello there"
    assert outcome.new_messages == [{"role": "assistant", "content": "hello there"}]
    assert any(isinstance(e, TextDeltaEvent) for e in events)


async def test_tool_call_turn_executes_and_iterates():
    tool_call = ToolCall(id="c1", name="echo", arguments={"text": "ping"})
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[tool_call])],
            [TextDelta(text="done"), ChatResult(content="done")],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    loop = AgentLoop(provider, tools)
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "run echo"}], emit)

    assert outcome.final_content == "done"
    roles = [m["role"] for m in outcome.new_messages]
    assert roles == ["assistant", "tool", "assistant"]
    assert outcome.new_messages[1]["content"] == "echo: ping"
    assert any(isinstance(e, ToolStarted) for e in events)
    assert any(isinstance(e, ToolFinished) and not e.is_error for e in events)
    # The second LLM call must include the tool result.
    assert provider.calls[1][-1]["role"] == "tool"


async def test_max_iterations_guard():
    endless_call = [
        ChatResult(content=None, tool_calls=[ToolCall(id="x", name="echo", arguments={"text": "again"})])
    ]
    provider = FakeProvider([list(endless_call) for _ in range(10)])
    tools = ToolRegistry()
    tools.register(EchoTool())
    loop = AgentLoop(provider, tools, max_iterations=3)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "loop"}], emit)

    assert outcome.reached_max_iterations
    assert outcome.final_content is None


async def test_unknown_tool_returns_error_to_model():
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[ToolCall(id="c", name="nope", arguments={})])],
            text_turn("recovered"),
        ]
    )
    loop = AgentLoop(provider, ToolRegistry())
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "x"}], emit)

    assert outcome.final_content == "recovered"
    assert any(isinstance(e, ToolFinished) and e.is_error for e in events)
