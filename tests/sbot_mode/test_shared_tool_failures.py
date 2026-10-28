"""Regressions for a shared (claw) tool failing inside an SBot turn.

Incident: render_diagram (a claw.tools.base.Tool) returned an Error, then the
SBot loop crashed reading `ends_turn_on_success`, which only sbot's base
declared; the runtime reported that crash as "could not reach the AI model".
"""

from typing import Any

import pytest

import claw.skills.render as render_module
from claw.tools.base import Tool as SharedTool
from claw.tools.diagram import RenderDiagramTool
from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.events import AgentEvent, ToolFinished, ToolStarted
from sbot.core.loop import AgentLoop
from sbot.core.memory import MemoryService
from sbot.core.runtime import AgentRuntime, TurnFailed
from sbot.i18n import t
from sbot.providers.base import ChatResult, ProviderError, ToolCall
from sbot.tools.base import Tool
from sbot.tools.registry import ToolRegistry
from tests.sbot_mode.conftest import FakeProvider, text_turn


class SharedEcho(SharedTool):
    name = "shared_echo"
    description = "Echo through the shared tool base"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def execute(self, text: str, **_: Any) -> str:
        return f"shared: {text}"


class Handoff(Tool):
    name = "handoff"
    description = "Durable handoff"
    parameters = {"type": "object", "properties": {}}
    ends_turn_on_success = True

    async def execute(self, **_: Any) -> str:
        return "Handed off"


def _tool_events(events: list[AgentEvent]) -> list[tuple[str, str]]:
    return [
        (type(e).__name__, e.tool) for e in events if isinstance(e, (ToolStarted, ToolFinished))
    ]


async def _run(registry: ToolRegistry, *calls: ToolCall, follow_up: str = "done", **kwargs):
    provider = FakeProvider([[ChatResult(content=None, tool_calls=list(calls))], text_turn(follow_up)])
    events: list[AgentEvent] = []
    outcome = await AgentLoop(provider, registry, **kwargs).run_turn(
        "t", [{"role": "user", "content": "go"}], events.append
    )
    return outcome, events, provider


def test_every_shared_tool_declares_the_sbot_loop_contract():
    assert SharedTool.ends_turn_on_success is False
    assert RenderDiagramTool.ends_turn_on_success is False


async def test_shared_tool_without_the_flag_does_not_crash_the_sbot_loop():
    registry = ToolRegistry()
    registry.register(SharedEcho())

    outcome, events, provider = await _run(
        registry, ToolCall(id="c1", name="shared_echo", arguments={"text": "hi"})
    )

    assert outcome.final_content == "done"
    assert len(provider.calls) == 2
    assert _tool_events(events) == [("ToolStarted", "shared_echo"), ("ToolFinished", "shared_echo")]


async def test_tool_that_ends_the_turn_still_ends_it_and_skips_bundled_calls():
    registry = ToolRegistry()
    registry.register(Handoff())
    registry.register(SharedEcho())

    outcome, events, provider = await _run(
        registry,
        ToolCall(id="c1", name="handoff", arguments={}),
        ToolCall(id="c2", name="shared_echo", arguments={"text": "must not run"}),
    )

    assert outcome.final_content == "Handed off"
    assert len(provider.calls) == 1
    assert _tool_events(events) == [("ToolStarted", "handoff"), ("ToolFinished", "handoff")]


async def test_renderer_failure_reaches_the_model_without_internals(monkeypatch, tmp_path):
    async def broken_renderer(*_args, **_kwargs):
        raise RuntimeError(
            "Traceback (most recent call last):\n  File \"/srv/claw/render_worker.py\"\n"
            "playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at "
            "/home/svc/.cache/ms-playwright/chromium_headless_shell-1228/chrome-linux/headless_shell"
        )

    monkeypatch.setattr(render_module, "render_diagram", broken_renderer)
    registry = ToolRegistry()
    registry.register(RenderDiagramTool(tmp_path, user_id="u1"))

    outcome, events, provider = await _run(
        registry,
        ToolCall(id="c1", name="render_diagram", arguments={"path": "a.html", "width": 1440, "height": 1140}),
        follow_up="PNG export is unavailable; here is the HTML source.",
    )

    assert outcome.final_content == "PNG export is unavailable; here is the HTML source."
    tool_message = next(m for m in provider.calls[1] if m.get("role") == "tool")
    result = tool_message["content"]
    assert result.startswith("Error: PNG rendering failed (the PNG renderer is not installed")
    assert "publish_artifact" in result
    for leaked in ("Traceback", "/home/svc", "ms-playwright", "render_worker.py"):
        assert leaked not in result
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert len(finished) == 1 and finished[0].is_error
    assert _tool_events(events) == [("ToolStarted", "render_diagram"), ("ToolFinished", "render_diagram")]


async def test_renderer_input_errors_stay_actionable(tmp_path):
    result = await RenderDiagramTool(tmp_path).execute(path="missing.html")
    assert result.startswith("Error: PNG rendering failed: Diagram file not found in workspace")


async def test_tool_blocked_by_the_arg_guard_is_reported_once():
    registry = ToolRegistry()
    registry.register(SharedEcho())

    outcome, events, _ = await _run(
        registry,
        ToolCall(id="c1", name="shared_echo", arguments={"text": "secret"}),
        arg_guard=lambda _name, args: (args, "Blocked by policy"),
    )

    assert outcome.final_content == "done"
    assert _tool_events(events) == [("ToolStarted", "shared_echo"), ("ToolFinished", "shared_echo")]
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.is_error and "Blocked by policy" in finished.result_preview


async def test_declined_project_action_never_appears_as_running():
    class Project(Tool):
        name = "project"
        description = "Manage a project"
        parameters = {
            "type": "object",
            "properties": {"project": {"type": "string"}, "action": {"type": "string"}},
            "required": ["project", "action"],
        }
        calls = 0

        async def execute(self, **_: Any) -> str:
            Project.calls += 1
            return "ran"

    registry = ToolRegistry()
    registry.register(Project())
    provider = FakeProvider([
        [ChatResult(content=None, tool_calls=[
            ToolCall(id="p", name="project", arguments={"project": "portal", "action": "compose_up"})
        ])],
        text_turn("Okay, not starting it."),
    ])
    events: list[AgentEvent] = []

    async def decline(*_args) -> bool:
        return False

    outcome = await AgentLoop(provider, registry).run_turn(
        "t", [{"role": "user", "content": "start"}], events.append, permission_mode="auto", confirm=decline
    )

    assert outcome.final_content == "Okay, not starting it."
    assert Project.calls == 0
    assert _tool_events(events) == []
    tool_message = next(m for m in provider.calls[1] if m.get("role") == "tool")
    assert "declined" in tool_message["content"]


async def test_approved_project_action_starts_and_finishes_once():
    class Project(Tool):
        name = "project"
        description = "Manage a project"
        parameters = {
            "type": "object",
            "properties": {"project": {"type": "string"}, "action": {"type": "string"}},
            "required": ["project", "action"],
        }

        async def execute(self, **_: Any) -> str:
            return "ran"

    registry = ToolRegistry()
    registry.register(Project())
    provider = FakeProvider([
        [ChatResult(content=None, tool_calls=[
            ToolCall(id="p", name="project", arguments={"project": "portal", "action": "compose_up"})
        ])],
        text_turn("Running."),
    ])
    events: list[AgentEvent] = []

    async def approve(*_args) -> bool:
        return True

    await AgentLoop(provider, registry).run_turn(
        "t", [{"role": "user", "content": "start"}], events.append, permission_mode="auto", confirm=approve
    )

    assert _tool_events(events) == [("ToolStarted", "project"), ("ToolFinished", "project")]


# ---------------------------------------------------------------- runtime errors


def _runtime(stores, provider, tmp_path) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(), users=stores["users"], bots=stores["bots"],
        sessions=stores["sessions"], messages=stores["messages"], memory=memory, audit=stores["audit"],
    )


async def _turn_error(stores, provider, tmp_path, email: str) -> str:
    runtime = _runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email(email)
    session = await stores["sessions"].create(user.id)
    with pytest.raises(TurnFailed) as failure:
        await runtime.handle_message(user.id, session.id, "hello")
    return failure.value.message


async def test_runtime_bug_is_not_reported_as_an_unreachable_model(stores, tmp_path):
    class BuggyProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise AttributeError("'RenderDiagramTool' object has no attribute 'ends_turn_on_success'")
            yield  # pragma: no cover

    message = await _turn_error(stores, BuggyProvider([]), tmp_path, "runtime-bug@sbot.ai")

    assert message == t("error.runtime", "en")
    assert "AI model could not be reached" not in message
    assert "ends_turn_on_success" not in message


async def test_provider_failure_is_still_reported_as_the_model(stores, tmp_path):
    class DownProvider(FakeProvider):
        async def stream_chat(self, *args, **kwargs):
            raise ProviderError("connection refused")
            yield  # pragma: no cover

    runtime = _runtime(stores, DownProvider([]), tmp_path)
    user = await stores["users"].get_or_create_by_email("provider-down@sbot.ai")
    session = await stores["sessions"].create(user.id)

    message = await runtime.handle_message(user.id, session.id, "hello")

    assert message.startswith("The AI model could not be reached")
    assert "connection refused" not in message


def test_runtime_message_is_localized():
    assert t("error.runtime", "th") != t("error.runtime", "en")
    assert "{" not in t("error.runtime", "th")


async def test_filesystem_permission_error_does_not_leak_host_paths(monkeypatch, tmp_path):
    async def unwritable(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", "/srv/claw/workspaces/u1/diagram-render-x.png")

    monkeypatch.setattr(render_module, "render_diagram", unwritable)
    result = await RenderDiagramTool(tmp_path).execute(path="a.html")

    assert result.startswith("Error: PNG rendering failed (the PNG renderer is unavailable)")
    assert "/srv" not in result and "Errno" not in result


async def test_fixable_render_rejections_reach_the_model(monkeypatch, tmp_path):
    async def too_big(*_args, **_kwargs):
        raise render_module.RenderRejected("Diagram needs more than 1 GB to render; simplify it")

    monkeypatch.setattr(render_module, "render_diagram", too_big)
    result = await RenderDiagramTool(tmp_path).execute(path="a.html")

    assert result.startswith("Error: PNG rendering failed: Diagram needs more than 1 GB")


async def test_worker_rejection_is_reported_as_fixable(tmp_path):
    (tmp_path / "plain.html").write_text("<html><body><p>no diagram here</p></body></html>")

    result = await RenderDiagramTool(tmp_path).execute(path="plain.html", width=640, height=480)

    assert result.startswith("Error: PNG rendering failed: Diagram must contain an SVG element")


def test_provider_error_message_keeps_non_retryable_cases():
    from sbot.core.runtime import _provider_error_message
    from sbot.i18n import is_no_tool_support_error, is_no_vision_support_error

    tool_detail = next(d for d in (
        "does not support tools", "tools is not supported", "tool use is not supported for this model",
    ) if is_no_tool_support_error(d))
    vision_detail = next(d for d in (
        "does not support image input", "image input is not supported", "model does not support images",
    ) if is_no_vision_support_error(d))

    assert _provider_error_message(ProviderError(tool_detail), "en") == t("error.llm_no_tool_support", "en")
    assert _provider_error_message(ProviderError(vision_detail), "en") == t("error.llm_no_vision_support", "en")
    assert _provider_error_message(ProviderError("connection refused"), "en").startswith(
        "The AI model could not be reached"
    )
