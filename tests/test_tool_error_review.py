"""Follow-ups to tool-failure recovery: what may be repeated, what the user is told,
and both loops agreeing on it. Every test drives the real claw and sbot loops,
registries or MCP proxies with fake providers and fake MCP sessions."""

import re
from typing import Any

import anyio
import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData
from pydantic_core import PydanticSerializationError

from claw.core.tool_errors import Category, SideEffect, tool_failure
from tests.conftest import text_turn
from tests.test_tool_error_recovery import IMPLS, assert_paired, call, run, tool_messages


@pytest.fixture(params=sorted(IMPLS))
def impl(request):
    return IMPLS[request.param]


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
