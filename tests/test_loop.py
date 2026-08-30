import asyncio
from typing import Any

from claw.core.events import AgentEvent, TextDeltaEvent, ToolFinished, ToolStarted
from claw.core.loop import (
    _DEFAULT_COMPACTION_CEILING_CHARS,
    _ESTIMATED_CHARS_PER_TOKEN,
    _IMAGE_BLOCK_CHARS,
    _MAX_TOOL_RESULT_CHARS,
    _RECENT_TOOL_RESULTS_KEPT,
    _REPEAT_LIMIT,
    _STALE_TOOL_RESULT_CHARS,
    AgentLoop,
    _prompt_size,
    visible_artifacts,
)
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


class CountingWriteTool(Tool):
    """Stands in for write_file: records every path it is actually asked to write."""

    name = "write_file"
    description = "Write a file"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self) -> None:
        self.writes: list[str] = []

    async def execute(self, path: str, content: str, **_: Any) -> str:
        self.writes.append(path)
        return f"Wrote {len(content)} chars to {path}"


async def test_rewriting_the_same_file_is_blocked_after_the_repeat_limit():
    # A model stuck "fixing" its own output rewrites the same path with slightly
    # different content every round, so the guard must key on the path.
    turns = [
        [
            ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"c{i}", name="write_file", arguments={"path": "report.py", "content": "x" * i}
                    )
                ],
            )
        ]
        for i in range(1, 7)
    ]
    provider = FakeProvider([*turns, text_turn("gave up rewriting")])
    tool = CountingWriteTool()
    tools = ToolRegistry()
    tools.register(tool)
    loop = AgentLoop(provider, tools, max_iterations=10)
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "make a report"}], emit)

    assert outcome.final_content == "gave up rewriting"
    assert tool.writes == ["report.py"] * _REPEAT_LIMIT
    blocked = [e for e in events if isinstance(e, ToolFinished) and e.is_error]
    assert len(blocked) == 6 - _REPEAT_LIMIT
    assert "Repeated call blocked" in blocked[0].result_preview
    # The model must see why, so it can move on instead of retrying blindly.
    tool_replies = [m["content"] for m in outcome.new_messages if m["role"] == "tool"]
    assert "Repeated call blocked" in tool_replies[-1]


class FakeRunTool(Tool):
    """Stands in for `exec`: a tool whose result changes when other tools edit
    the thing it reads, so repeating it is legitimate."""

    name = "exec"
    description = "Run a command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def __init__(self) -> None:
        self.runs = 0

    async def execute(self, command: str, **_: Any) -> str:
        self.runs += 1
        return f"[exit code: 1] run {self.runs}"


async def test_the_edit_then_rerun_cycle_is_not_treated_as_a_loop():
    # exec → edit → exec → edit → … repeats the same command, but the file it
    # runs changed in between, so the result genuinely differs each time.
    run = ToolCall(id="r", name="exec", arguments={"command": "python report.py"})
    fix = ToolCall(id="w", name="write_file", arguments={"path": "report.py", "content": "print(1)"})
    script: list[list[Any]] = []
    for _ in range(4):
        script.append([ChatResult(content=None, tool_calls=[run])])
        script.append([ChatResult(content=None, tool_calls=[fix])])
    provider = FakeProvider([*script, text_turn("fixed it")])
    exec_tool = FakeRunTool()
    write_tool = CountingWriteTool()
    tools = ToolRegistry()
    tools.register(exec_tool)
    tools.register(write_tool)
    loop = AgentLoop(provider, tools, max_iterations=20)
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "build a report"}], emit)

    assert outcome.final_content == "fixed it"
    # Nothing may be blocked: every repeat had a different call in between.
    assert exec_tool.runs == 4
    assert len(write_tool.writes) == 4
    assert not [e for e in events if isinstance(e, ToolFinished) and e.is_error]


async def test_declined_calls_do_not_count_toward_the_repeat_limit():
    # In ask mode the user may decline the same command repeatedly while
    # deciding; those calls never ran, so they must not exhaust the limit and
    # silently stop the prompt from ever appearing again.
    call = ToolCall(id="x", name="exec", arguments={"command": "rm -rf build"})
    attempts = _REPEAT_LIMIT + 2
    provider = FakeProvider(
        [[ChatResult(content=None, tool_calls=[call])] for _ in range(attempts)] + [text_turn("ok, skipped")]
    )
    exec_tool = FakeRunTool()
    tools = ToolRegistry()
    tools.register(exec_tool)
    loop = AgentLoop(provider, tools, max_iterations=20)
    _, emit = collector()
    asked = 0

    async def confirm(_turn_id: str, _tool: str, _preview: str) -> bool:
        nonlocal asked
        asked += 1
        return False

    outcome = await loop.run_turn(
        "t1",
        [{"role": "user", "content": "clean up"}],
        emit,
        permission_mode="ask",
        confirm=confirm,
    )

    assert outcome.final_content == "ok, skipped"
    # The user keeps getting asked; the guard never hides the prompt.
    assert asked == attempts
    assert exec_tool.runs == 0
    tool_replies = [m["content"] for m in outcome.new_messages if m["role"] == "tool"]
    assert all("Repeated call blocked" not in reply for reply in tool_replies)


async def test_approved_repeats_in_ask_mode_are_still_confirmed_not_auto_blocked():
    # A user may legitimately approve the same exec command several times in a
    # row (e.g. re-running a flaky test). The repeat-breaker must never take
    # over from the ask-mode gate and silently stop prompting — the human
    # approving each call is this tool's safety gate, not the loop.
    call = ToolCall(id="x", name="exec", arguments={"command": "pytest -k flaky"})
    attempts = _REPEAT_LIMIT + 2
    provider = FakeProvider(
        [[ChatResult(content=None, tool_calls=[call])] for _ in range(attempts)] + [text_turn("done")]
    )
    exec_tool = FakeRunTool()
    tools = ToolRegistry()
    tools.register(exec_tool)
    loop = AgentLoop(provider, tools, max_iterations=20)
    _, emit = collector()
    asked = 0

    async def confirm(_turn_id: str, _tool: str, _preview: str) -> bool:
        nonlocal asked
        asked += 1
        return True

    outcome = await loop.run_turn(
        "t1",
        [{"role": "user", "content": "run the flaky test a few times"}],
        emit,
        permission_mode="ask",
        confirm=confirm,
    )

    assert outcome.final_content == "done"
    # Every single attempt must prompt, even past the repeat limit.
    assert asked == attempts
    assert exec_tool.runs == attempts
    tool_replies = [m["content"] for m in outcome.new_messages if m["role"] == "tool"]
    assert all("Repeated call blocked" not in reply for reply in tool_replies)


class CountingEditTool(Tool):
    """Stands in for edit_file: records every path it is actually asked to edit."""

    name = "edit_file"
    description = "Edit a file"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def execute(self, path: str, old_text: str, new_text: str, **_: Any) -> str:
        self.edits.append(path)
        return f"Error: old_text not found in {path}"


async def test_edit_file_repeats_on_same_path_are_blocked_like_write_file():
    # A model stuck guessing old_text wrong retries edit_file on the same path
    # with a different fragment each time — the same failure mode write_file
    # is guarded against, so it must be keyed on path the same way.
    turns = [
        [
            ChatResult(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"c{i}",
                        name="edit_file",
                        arguments={"path": "report.py", "old_text": f"guess {i}", "new_text": "fixed"},
                    )
                ],
            )
        ]
        for i in range(1, 7)
    ]
    provider = FakeProvider([*turns, text_turn("gave up editing")])
    tool = CountingEditTool()
    tools = ToolRegistry()
    tools.register(tool)
    loop = AgentLoop(provider, tools, max_iterations=10)
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "fix the report"}], emit)

    assert outcome.final_content == "gave up editing"
    assert tool.edits == ["report.py"] * _REPEAT_LIMIT
    blocked = [
        e for e in events if isinstance(e, ToolFinished) and "Repeated call blocked" in e.result_preview
    ]
    assert len(blocked) == 6 - _REPEAT_LIMIT


async def test_repeat_guard_counts_distinct_calls_separately():
    calls = [ToolCall(id=str(i), name="echo", arguments={"text": f"n{i}"}) for i in range(_REPEAT_LIMIT + 2)]
    provider = FakeProvider(
        [[ChatResult(content=None, tool_calls=[c])] for c in calls] + [text_turn("all done")]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    loop = AgentLoop(provider, tools, max_iterations=10)
    events, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "echo a few things"}], emit)

    assert outcome.final_content == "all done"
    assert not [e for e in events if isinstance(e, ToolFinished) and e.is_error]


class SlowEchoTool(EchoTool):
    async def execute(self, text: str, **_: Any) -> str:
        await asyncio.sleep(0.05)
        return f"echo: {text}"


async def test_turn_stops_when_the_time_budget_runs_out():
    endless = [
        [ChatResult(content=None, tool_calls=[ToolCall(id="x", name="echo", arguments={"text": "again"})])]
        for _ in range(10)
    ]
    provider = FakeProvider(endless)
    tools = ToolRegistry()
    tools.register(SlowEchoTool())
    loop = AgentLoop(provider, tools, max_iterations=10, max_turn_seconds=0.01)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "loop"}], emit)

    assert outcome.timed_out
    # Time, not the step limit, is what stopped it — the user gets told so.
    assert not outcome.reached_max_iterations
    # The budget is checked between steps, so the first step still completes.
    assert outcome.iterations == 1
    assert len(provider.calls) == 1


class BigResultTool(Tool):
    """Stands in for web search/extract: returns far more text than the model
    needs, which the loop must not re-send in full on every later iteration."""

    name = "big"
    description = "Return a lot of text"
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}

    async def execute(self, n: int = 0, **_: Any) -> str:
        return f"[{n}]" + "x" * (_MAX_TOOL_RESULT_CHARS * 2)


def _tool_contents(call: list[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in call if m.get("role") == "tool"]


async def test_oversized_tool_results_are_capped_in_the_prompt_only():
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[ToolCall(id="c1", name="big", arguments={"n": 1})])],
            text_turn("done"),
        ]
    )
    tools = ToolRegistry()
    tools.register(BigResultTool())
    loop = AgentLoop(provider, tools, max_iterations=5)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "search"}], emit)

    assert outcome.final_content == "done"
    sent = _tool_contents(provider.calls[1])[0]
    assert len(sent) < _MAX_TOOL_RESULT_CHARS + 400
    assert "truncated" in sent
    # The stored transcript and the UI still get everything the tool returned.
    stored = [m for m in outcome.new_messages if m["role"] == "tool"][0]["content"]
    assert len(stored) > _MAX_TOOL_RESULT_CHARS * 2


def _big_tool_turns(n: int) -> list[list[Any]]:
    return [
        [ChatResult(content=None, tool_calls=[ToolCall(id=f"c{i}", name="big", arguments={"n": i})])]
        for i in range(n)
    ]


def _rewritten_messages(calls: list[list[dict[str, Any]]]) -> int:
    """How many times a message the provider was ALREADY sent came back changed.
    Every one of those forfeits the provider's prompt cache for the whole prefix
    before it, so this is the number the loop has to keep near zero."""
    rewrites = 0
    for previous, current in zip(calls, calls[1:]):
        rewrites += sum(1 for old, new in zip(previous, current) if old != new)
    return rewrites


async def test_the_prompt_grows_by_appending_so_the_provider_cache_survives():
    provider = FakeProvider(_big_tool_turns(_RECENT_TOOL_RESULTS_KEPT + 2) + [text_turn("done")])
    tools = ToolRegistry()
    tools.register(BigResultTool())
    loop = AgentLoop(provider, tools, max_iterations=20)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "search a lot"}], emit)

    assert outcome.final_content == "done"
    # Well under the compaction ceiling, so nothing already sent may be rewritten.
    assert _rewritten_messages(provider.calls) == 0
    final_prompt = _tool_contents(provider.calls[-1])
    assert len(final_prompt) == _RECENT_TOOL_RESULTS_KEPT + 2
    assert all(_MAX_TOOL_RESULT_CHARS < len(c) < _MAX_TOOL_RESULT_CHARS + 400 for c in final_prompt)
    # Trimming rewrites contents but never drops a message, so every tool result
    # stays paired with the tool call that produced it.
    assert len(provider.calls[-1]) == len(_tool_contents(provider.calls[-1])) * 2 + 1


async def test_stale_tool_results_are_compacted_once_the_prompt_gets_huge():
    provider = FakeProvider(_big_tool_turns(_RECENT_TOOL_RESULTS_KEPT + 2) + [text_turn("done")])
    tools = ToolRegistry()
    tools.register(BigResultTool())
    loop = AgentLoop(provider, tools, max_iterations=20)
    _, emit = collector()

    # A prompt already over the ceiling before the first tool call: past that
    # point fitting the context window outranks keeping the cached prefix.
    huge = [{"role": "user", "content": "x" * (_DEFAULT_COMPACTION_CEILING_CHARS + 1)}]
    outcome = await loop.run_turn("t1", huge, emit)

    assert outcome.final_content == "done"
    final_prompt = _tool_contents(provider.calls[-1])
    # Oldest two have aged out of the window; the newest four are still full-size.
    assert all(len(c) < _STALE_TOOL_RESULT_CHARS + 400 for c in final_prompt[:2])
    assert all(len(c) > _MAX_TOOL_RESULT_CHARS for c in final_prompt[2:])
    # Compaction only ever shrinks: a result that aged out never comes back.
    assert all(len(c) < _STALE_TOOL_RESULT_CHARS + 400 for c in _tool_contents(provider.calls[-2])[:1])


async def test_the_compaction_ceiling_follows_the_model_context_window():
    async def run(model: str) -> list[str]:
        provider = FakeProvider(_big_tool_turns(_RECENT_TOOL_RESULTS_KEPT + 2) + [text_turn("done")])
        tools = ToolRegistry()
        tools.register(BigResultTool())
        loop = AgentLoop(provider, tools, max_iterations=20)
        _, emit = collector()
        await loop.run_turn("t1", [{"role": "user", "content": "search"}], emit, model=model)
        return _tool_contents(provider.calls[-1])

    # ~72k chars of tool results: comfortably inside a million-token window, far
    # outside an 8k one. The same turn must compact on one and not the other.
    roomy = await run("openrouter/anthropic/claude-sonnet-5")
    assert all(len(c) > _MAX_TOOL_RESULT_CHARS for c in roomy)

    cramped = await run("gpt-4")
    assert all(len(c) < _STALE_TOOL_RESULT_CHARS + 400 for c in cramped[:2])


async def test_admin_context_window_override_beats_the_lookup_table():
    async def run(model: str, override: int | None) -> list[str]:
        provider = FakeProvider(_big_tool_turns(_RECENT_TOOL_RESULTS_KEPT + 2) + [text_turn("done")])
        tools = ToolRegistry()
        tools.register(BigResultTool())
        loop = AgentLoop(provider, tools, max_iterations=20)
        _, emit = collector()
        await loop.run_turn(
            "t1",
            [{"role": "user", "content": "search"}],
            emit,
            model=model,
            context_window=override,
        )
        return _tool_contents(provider.calls[-1])

    # gpt-4's real 8k window compacts this turn; an admin who says the id behind
    # their gateway actually has a million-token window must not be second-guessed.
    assert all(len(c) > _MAX_TOOL_RESULT_CHARS for c in await run("gpt-4", 1_000_000))
    # And a model the lookup table has never heard of gets the admin's number
    # instead of the conservative default ceiling.
    cramped = await run("private-gateway/mystery-1", 8_000)
    assert all(len(c) < _STALE_TOOL_RESULT_CHARS + 400 for c in cramped[:2])


async def test_tool_results_from_earlier_turns_are_never_rewritten():
    provider = FakeProvider(_big_tool_turns(1) + [text_turn("done")])
    tools = ToolRegistry()
    tools.register(BigResultTool())
    loop = AgentLoop(provider, tools, max_iterations=5)
    _, emit = collector()

    # Earlier turns were already capped when they were stored. Re-trimming them
    # would drop context the model was given last turn and invalidate a prefix
    # the provider is very likely still caching.
    old_result = "y" * (_DEFAULT_COMPACTION_CEILING_CHARS + 1)
    history = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old", "function": {}}]},
        {"role": "tool", "tool_call_id": "old", "name": "big", "content": old_result},
        {"role": "user", "content": "now"},
    ]

    await loop.run_turn("t1", history, emit)

    assert all(_tool_contents(call)[0] == old_result for call in provider.calls)


class StallingProvider(FakeProvider):
    """Streams a little text, then hangs — a provider that is slow to answer,
    not a tool that is slow to run."""

    async def stream_chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        yield TextDelta(text="partial answer")
        await asyncio.sleep(5)
        yield ChatResult(content="never reached")


async def test_slow_provider_stream_is_cut_off_mid_call_by_the_budget():
    provider = StallingProvider([])
    loop = AgentLoop(provider, ToolRegistry(), max_turn_seconds=0.05)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "hi"}], emit)

    assert outcome.timed_out
    # Text the user already saw is kept as the answer rather than discarded for
    # a bare timeout message, and it is persisted so a reload still shows it.
    assert outcome.final_content == "partial answer"
    assert outcome.new_messages[-1] == {"role": "assistant", "content": "partial answer"}


async def test_a_cut_off_call_is_still_billed_an_estimate():
    # The provider reports usage only in its final chunk, which never arrives
    # here — but it billed the prompt. Recording zero would make a turn that
    # always times out cost the user nothing against their quota.
    provider = StallingProvider([])
    loop = AgentLoop(provider, ToolRegistry(), max_turn_seconds=0.05)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "x" * 4000}], emit)

    assert outcome.timed_out
    assert outcome.usage["prompt_tokens"] >= 4000 / _ESTIMATED_CHARS_PER_TOKEN
    assert outcome.usage["completion_tokens"] == int(len("partial answer") / _ESTIMATED_CHARS_PER_TOKEN)


async def test_time_budget_of_zero_disables_the_cap():
    provider = FakeProvider([text_turn("hello")])
    loop = AgentLoop(provider, ToolRegistry(), max_turn_seconds=0)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "hi"}], emit)

    assert outcome.final_content == "hello"
    assert not outcome.timed_out


class FakeExecTool(Tool):
    """Stands in for the sandbox exec tool: writes a file into the workspace."""

    name = "exec"
    description = "Run a command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def __init__(self, workspace):
        self.workspace = workspace

    async def execute(self, command: str, **_: Any) -> str:
        (self.workspace / "btc_30d_chart.png").write_bytes(b"\x89PNG fake")
        return "[exit code: 0]"


async def test_exec_created_files_are_surfaced_as_artifacts(tmp_path):
    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="exec", arguments={"command": "make chart"})],
                )
            ],
            text_turn("chart ready"),
        ]
    )
    tools = ToolRegistry()
    tools.register(FakeExecTool(tmp_path))
    loop = AgentLoop(provider, tools, workspace=tmp_path)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "chart"}], emit)

    # The file the exec command wrote should be offered as a downloadable artifact.
    assert "btc_30d_chart.png" in outcome.artifacts


async def test_exec_created_intermediate_files_are_not_artifacts(tmp_path):
    """A script/JSON/XML an exec command produces on the way to the real
    deliverable must stay out of the artifacts list — non-technical users read
    every chip as 'download this'. The file itself is untouched on disk."""

    class ScriptyExecTool(Tool):
        name = "exec"
        description = "writes files"
        parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

        def __init__(self, workspace):
            self.workspace = workspace

        async def execute(self, command: str, **_: Any) -> str:
            (self.workspace / "build_report.py").write_text("print('hi')\n")
            (self.workspace / "data.json").write_text("{}\n")
            (self.workspace / "config.xml").write_text("<x/>\n")
            (self.workspace / "report.pdf").write_bytes(b"%PDF fake")
            return "[exit code: 0]"

    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="exec", arguments={"command": "build"})],
                )
            ],
            text_turn("done"),
        ]
    )
    tools = ToolRegistry()
    tools.register(ScriptyExecTool(tmp_path))
    loop = AgentLoop(provider, tools, workspace=tmp_path)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "report"}], emit)

    assert outcome.artifacts == ["report.pdf"]
    # The helper files stay in the workspace, reachable by direct URL / tools.
    assert (tmp_path / "build_report.py").is_file()
    assert (tmp_path / "data.json").is_file()


async def test_template_and_b64_assembly_inputs_are_not_artifacts(tmp_path):
    """A '*template*.html' the agent scaffolds the real report from, and a
    base64 payload meant to be inlined, are assembly inputs — not deliverables.
    Showing both produced two near-identical preview cards and users opened
    the placeholder one."""

    class TemplateExecTool(Tool):
        name = "exec"
        description = "writes template then final report"
        parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

        def __init__(self, workspace):
            self.workspace = workspace

        async def execute(self, command: str, **_: Any) -> str:
            (self.workspace / "water_report_nan_template.html").write_text("<html>placeholder map</html>")
            (self.workspace / "map_b64.txt").write_text("AAAA")
            (self.workspace / "map_kalasin.b64").write_text("BBBB")
            (self.workspace / "osm_map_base64.txt").write_text("CCCC")
            (self.workspace / "water_report_nan.html").write_text("<html>FINAL with real map</html>")
            return "[exit code: 0]"

    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="exec", arguments={"command": "build"})],
                )
            ],
            text_turn("done"),
        ]
    )
    tools = ToolRegistry()
    tools.register(TemplateExecTool(tmp_path))
    loop = AgentLoop(provider, tools, workspace=tmp_path)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "report"}], emit)

    assert outcome.artifacts == ["water_report_nan.html"]
    # Suppressed, not forgotten: a turn that dies before it can speak still has
    # to be able to name what it wrote.
    assert sorted(outcome.hidden_artifacts) == [
        "map_b64.txt",
        "map_kalasin.b64",
        "osm_map_base64.txt",
        "water_report_nan_template.html",
    ]


def test_hidden_artifact_rules_do_not_swallow_deliverables():
    """The markers are matched as whole name tokens, and a template only counts
    as an intermediate when something else was assembled from it. Matching
    "b64"/"template" as bare substrings hid files the user asked for outright —
    and there is no workspace browser to recover them from."""
    # "b64" inside a hex id — including the "generated-<hex>.png" names the
    # image route mints, which collide roughly 1 in 680.
    assert visible_artifacts(["chart_9b64c1.png"]) == ["chart_9b64c1.png"]
    assert visible_artifacts(["generated-a1b64f2d.png"]) == ["generated-a1b64f2d.png"]
    assert visible_artifacts(["db64.zip"]) == ["db64.zip"]
    # A template with nothing built from it IS the deliverable.
    assert visible_artifacts(["invoice_template.xlsx"]) == ["invoice_template.xlsx"]
    assert visible_artifacts(["email_template.html"]) == ["email_template.html"]
    assert visible_artifacts(["Template.pdf"]) == ["Template.pdf"]
    # A template shadowed by a sibling of a *different* type is still a
    # deliverable — the report wasn't assembled from it.
    assert visible_artifacts(["invoice_template.xlsx", "notes.pdf"]) == [
        "invoice_template.xlsx",
        "notes.pdf",
    ]
    # Real assembly inputs still go.
    assert visible_artifacts(["osm_map_base64.txt", "map.b64", "build.py"]) == []


async def test_write_file_intermediate_types_are_not_artifacts(tmp_path):
    """write_file follows the same rule as exec diffing: .py/.json/.xml are
    helpers, not deliverables, and never become download chips."""

    class WriteTool(Tool):
        name = "write_file"
        description = "write a file"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, path: str, content: str = "", **_: Any) -> str:
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"wrote {path}"

    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[
                        ToolCall(id="c1", name="write_file", arguments={"path": "notes.py", "content": "x=1"}),
                        ToolCall(id="c2", name="write_file", arguments={"path": "out/report.pdf", "content": "%PDF"}),
                    ],
                )
            ],
            text_turn("here is your pdf"),
        ]
    )
    tools = ToolRegistry()
    tools.register(WriteTool())
    loop = AgentLoop(provider, tools, workspace=tmp_path)
    _, emit = collector()

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "pdf please"}], emit)

    assert outcome.artifacts == ["out/report.pdf"]


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


def test_prompt_size_estimates_images_instead_of_measuring_base64():
    """A few megabytes of base64 is ~1.5k tokens to the model. Measuring the
    data URL literally would put every attachment turn permanently over the
    compaction ceiling and, on a timed-out turn, bill imaginary prompt tokens."""
    data_url = "data:image/png;base64," + "A" * 2_000_000
    prompt = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": "what is this?"},
            ],
        }
    ]

    assert _prompt_size(prompt) == _IMAGE_BLOCK_CHARS + len("what is this?")
