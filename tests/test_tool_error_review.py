"""Follow-ups to tool-failure recovery: what may be repeated, what the user is told,
and both loops agreeing on it. Every test drives the real claw and sbot loops,
registries or MCP proxies with fake providers and fake MCP sessions."""

import re
from typing import Any

import pytest

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
