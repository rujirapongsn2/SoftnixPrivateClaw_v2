"""Follow-ups to tool-failure recovery: what may be repeated, what the user is told,
and both loops agreeing on it. Every test drives the real claw and sbot loops,
registries or MCP proxies with fake providers and fake MCP sessions."""

import re
from types import SimpleNamespace
from typing import Any

import anyio
import httpx
import pytest
from loguru import logger
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData, ToolAnnotations
from pydantic_core import PydanticSerializationError

from claw.core.tool_errors import Category, SideEffect, tool_failure
from tests.conftest import text_turn
from claw.tools.base import ToolError as ClawToolError
from sbot.tools.base import ToolError as SbotToolError
from tests.test_tool_error_recovery import IMPLS, assert_paired, call, counting_tool, run, tool_messages

TOOL_ERRORS = {"claw": ClawToolError, "sbot": SbotToolError}


@pytest.fixture(params=sorted(IMPLS))
def impl(request):
    return SimpleNamespace(**vars(IMPLS[request.param]), tool_error=TOOL_ERRORS[request.param])


def category_of(text: str) -> str:
    return re.search(r"\[(\w+);", text).group(1)


def unknown_outcome_tool(impl, name):
    """Every call reports that the request may have reached the remote."""

    class Uncertain(impl.base):
        description = "Write somewhere remote"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}
        calls = 0

        async def execute(self, **_: Any) -> str:
            type(self).calls += 1
            return tool_failure(name, Category.OUTCOME_UNKNOWN, "connection reset", side_effect=SideEffect.UNKNOWN)

    Uncertain.name = name
    return Uncertain()


# ---------------------------------------------------------------- loop parity


async def test_both_loops_refuse_exactly_the_same_repeats():
    """A path-keyed tool's call with new content is not an identical retry in either loop."""

    async def scenario(impl):
        tool = unknown_outcome_tool(impl, "write_file")
        registry = impl.registry()
        registry.register(tool)
        _, events, provider = await run(
            impl, registry,
            call("c1", "write_file", path="a.txt", content="one"),
            call("c2", "write_file", path="a.txt", content="two"),
            call("c3", "write_file", path="a.txt", content="two"),
            text_turn("unsure"),
        )
        results = [tool_messages(provider, i)[-1]["content"] for i in (1, 2, 3)]
        assert_paired(impl, events)
        return tool.calls, [(category_of(r), "Not run again" in r) for r in results]

    claw_outcome = await scenario(IMPLS["claw"])
    sbot_outcome = await scenario(IMPLS["sbot"])

    assert claw_outcome == (
        2, [("outcome_unknown", False), ("outcome_unknown", False), ("outcome_unknown", True)]
    )
    assert sbot_outcome == claw_outcome


# ---------------------------------------------------------------- MCP: sent or not


class RaisingSession:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    async def call_tool(self, name, kwargs, read_timeout_seconds=None):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [anyio.ClosedResourceError(), PydanticSerializationError("cannot serialize argument")],
    ids=["closed-session", "serialization"],
)
async def test_a_request_that_was_never_sent_is_retryable(impl, exc):
    session = RaisingSession(exc)
    proxy = impl.proxy(session, "linear", "create_issue", "Create", {"type": "object"})
    registry = impl.registry()
    registry.register(proxy)

    _, events, provider = await run(
        impl, registry,
        call("c1", "mcp_linear_create_issue", title="Bug"),
        call("c2", "mcp_linear_create_issue", title="Bug"),
        text_turn("done"),
    )

    first = tool_messages(provider, 1)[0]["content"]
    assert category_of(first) == "temporarily_unavailable"
    assert "side_effect: none" in first and "retryable: yes" in first
    assert session.calls == 2  # the identical retry was sent, not refused
    assert "Not run again" not in tool_messages(provider, 2)[1]["content"]
    assert_paired(impl, events)


@pytest.mark.parametrize(
    "exc",
    [
        anyio.BrokenResourceError(),
        ConnectionResetError("reset mid-request"),
        McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed")),
        McpError(ErrorData(code=httpx.codes.REQUEST_TIMEOUT, message="Timed out")),
    ],
    ids=["broken-stream", "reset", "sdk-connection-closed", "timeout"],
)
async def test_a_request_that_may_have_reached_the_server_is_not_repeated(impl, exc):
    session = RaisingSession(exc)
    proxy = impl.proxy(session, "linear", "create_issue", "Create", {"type": "object"})
    registry = impl.registry()
    registry.register(proxy)

    _, _, provider = await run(
        impl, registry,
        call("c1", "mcp_linear_create_issue", title="Bug"),
        call("c2", "mcp_linear_create_issue", title="Bug"),
        text_turn("unsure"),
    )

    first = tool_messages(provider, 1)[0]["content"]
    assert category_of(first) == "outcome_unknown" and "side_effect: unknown" in first
    assert session.calls == 1
    assert "Not run again" in tool_messages(provider, 2)[1]["content"]


# ---------------------------------------------------------------- repeat-safe tools


@pytest.mark.parametrize(
    "annotations, sent",
    [
        (ToolAnnotations(readOnlyHint=True), 2),
        (ToolAnnotations(readOnlyHint=False, idempotentHint=True), 2),
        (ToolAnnotations(readOnlyHint=False, destructiveHint=True), 1),
        (None, 1),
    ],
    ids=["read-only", "idempotent", "destructive", "unannotated"],
)
async def test_only_a_tool_that_may_add_an_effect_is_refused_after_a_timeout(impl, annotations, sent):
    session = RaisingSession(McpError(ErrorData(code=httpx.codes.REQUEST_TIMEOUT, message="Timed out")))
    proxy = impl.proxy(
        session, "linear", "list_issues", "List", {"type": "object"}, annotations=annotations
    )
    registry = impl.registry()
    registry.register(proxy)

    _, _, provider = await run(
        impl, registry,
        call("c1", "mcp_linear_list_issues", q="open"),
        call("c2", "mcp_linear_list_issues", q="open"),
        text_turn("done"),
    )

    first = tool_messages(provider, 1)[0]["content"]
    assert category_of(first) == "outcome_unknown"
    # What the model is told matches what the loop will allow.
    assert ("retryable: yes" in first) is (sent == 2)
    assert ("Do not call it again in this turn" in first) is (sent == 1)
    assert session.calls == sent
    assert ("Not run again" in tool_messages(provider, 2)[1]["content"]) is (sent == 1)


# ---------------------------------------------------------------- what the model sees


def raising_tool(impl, exc: BaseException):
    tool = counting_tool(impl, "lookup")

    async def execute(**_: Any) -> str:
        raise exc

    tool.execute = execute
    return tool


async def test_sandbox_paths_stay_visible_while_host_paths_and_secrets_are_scrubbed(impl):
    exc = FileNotFoundError(
        2,
        "No such file or directory (loaded from /srv/claw-host/app/loader.py, "
        "key=sk-test-SYNTHETIC0001; also tried data/report.csv and /workspace2/x.csv)",
        "/workspace/data/report.csv",
    )
    registry = impl.registry()
    registry.register(raising_tool(impl, exc))

    _, _, provider = await run(impl, registry, call("c1", "lookup", q="a"), text_turn("ok"))

    content = tool_messages(provider, 1)[0]["content"]
    assert "'/workspace/data/report.csv'" in content
    assert "data/report.csv" in content
    assert "/srv/claw-host" not in content and "sk-test-SYNTHETIC0001" not in content
    assert "/workspace2/x.csv" not in content


async def test_our_own_tool_error_keeps_its_guidance_and_remote_text_stays_short(impl):
    guidance = "Use one of: " + ", ".join(f"option_{i}" for i in range(150)) + ". token=SYNTHETIC-SECRET-1"
    assert 1200 < len(guidance) < 2000
    own = raising_tool(impl, impl.tool_error(guidance))
    registry = impl.registry()
    registry.register(own)

    _, _, provider = await run(impl, registry, call("c1", "lookup", q="a"), text_turn("ok"))

    content = tool_messages(provider, 1)[0]["content"]
    assert "option_149." in content
    assert "SYNTHETIC-SECRET-1" not in content

    class LongRemote:
        async def call_tool(self, name, kwargs, read_timeout_seconds=None):
            return SimpleNamespace(content=[SimpleNamespace(text=guidance)], isError=True)

    registry = impl.registry()
    registry.register(impl.proxy(LongRemote(), "kb", "search", "Search", {"type": "object"}))
    _, _, provider = await run(impl, registry, call("c1", "mcp_kb_search", q="a"), text_turn("ok"))

    remote = tool_messages(provider, 1)[0]["content"]
    assert "option_10," in remote and "option_149" not in remote


# ---------------------------------------------------------------- server logs


@pytest.fixture
def log_lines():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(sink)


async def test_a_turn_of_refusals_writes_no_tool_failure_warnings(impl, log_lines):
    shell = counting_tool(impl, "exec")
    shell.parameters = {"type": "object", "properties": {"command": {"type": "string"}}}
    registry = impl.registry()
    registry.register(shell)

    async def decline(_turn, _name, _preview) -> bool:
        return False

    def guard(name, args):
        return args, "masked by the guardrail: password=hunter2-SYNTHETIC"

    await run(
        impl, registry,
        call("c1", "exec", command="rm -rf /tmp/x"),
        call("c2", "exec", command="rm -rf /tmp/y"),
        text_turn("I did not run those."),
        permission_mode="ask", confirm=decline,
    )
    await run(impl, registry, call("c3", "exec", command="ls"), text_turn("blocked"), arg_guard=guard)

    assert shell.calls == 0
    assert not [line for line in log_lines if line.startswith("WARNING") and "Tool failure" in line]
    assert not [line for line in log_lines if "hunter2-SYNTHETIC" in line]


async def test_a_real_tool_failure_logs_once_with_its_stack_and_without_credentials(impl, log_lines):
    def connect():
        raise RuntimeError(
            "login failed for postgresql://admin:hunter2-SYNTHETIC@db.internal/app key=sk-test-SYNTHETIC0001"
        )

    tool = counting_tool(impl, "lookup")

    async def execute(**_: Any) -> str:
        connect()

    tool.execute = execute
    registry = impl.registry()
    registry.register(tool)

    _, _, provider = await run(impl, registry, call("c1", "lookup", q="a"), text_turn("failed"))

    error_id = re.search(r"error_id=(te_[0-9a-f]{10})", tool_messages(provider, 1)[0]["content"]).group(1)
    [line] = [line for line in log_lines if "Tool failure" in line]
    assert line.startswith("WARNING") and error_id in line
    assert "Traceback (most recent call last)" in line and "in connect" in line
    assert "login failed for postgresql://***@db.internal/app" in line
    assert "hunter2-SYNTHETIC" not in line and "sk-test-SYNTHETIC0001" not in line
