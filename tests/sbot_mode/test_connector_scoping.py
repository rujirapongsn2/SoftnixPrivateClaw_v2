"""Connector tools are registered per registry but cached per user.

A user has one set of connectors and one ToolRegistry per bot, so the two
scopes disagree — and both bugs these cover come from that disagreement: a
registry that never received the tools, and a guard that stopped seeing the
names already in one.
"""

from typing import Any

import pytest

from sbot.core.connectors import ConnectorManager, _populate, _register_scoped, _UserConnections
from sbot.tools.base import Tool
from sbot.tools.registry import ToolRegistry


class _StubTool(Tool):
    description = "stub"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, name: str, marker: str):
        self.name = name
        self.marker = marker

    async def execute(self, **_: Any) -> str:
        return self.marker


def test_a_restricted_bot_does_not_let_a_connector_take_a_name_already_in_use():
    """The guard asks whether the name is free, which is not the same question
    as whether the current bot may call it. Filtering by the allowlist made
    every connector name read as free, so the user's own connector overwrote
    the admin-global tool holding it — a silent credential swap for anyone
    calling that name, including the user's other bots."""
    registry = ToolRegistry()
    registry.restrict_to(["web_search"])
    state = _UserConnections()
    admin_global = _StubTool("api_hr_get_salary", "global")
    users_own = _StubTool("api_hr_get_salary", "user")

    assert _register_scoped(registry, state, admin_global, "hr") is True
    assert _register_scoped(registry, state, users_own, "hr-lookalike") is False

    assert state.tools == [admin_global]
    assert state.tool_names == ["api_hr_get_salary"]


def test_the_second_bot_of_a_user_gets_the_tools_the_first_bot_connected():
    """Connecting happens once per user and is cached by signature, so a second
    bot arrives with everything connected and nothing registered where it can
    see it. It used to be handed an empty registry and report the connector
    unreachable while the UI showed it connected."""
    first_bot = ToolRegistry()
    second_bot = ToolRegistry()
    state = _UserConnections()
    tool = _StubTool("mcp_gmail_send", "proxy")
    _register_scoped(first_bot, state, tool, "gmail")

    _populate(second_bot, state)

    assert second_bot.has("mcp_gmail_send")
    # Mirroring is not a second registration: the names are the same names.
    assert state.tool_names == ["mcp_gmail_send"]


def test_mirroring_does_not_overwrite_a_name_the_target_registry_already_holds():
    state = _UserConnections()
    _register_scoped(ToolRegistry(), state, _StubTool("read_file", "connector"), "files")
    builtin = _StubTool("read_file", "builtin")
    target = ToolRegistry()
    target.register(builtin)

    _populate(target, state)

    assert target.get("read_file") is builtin


@pytest.mark.asyncio
async def test_closing_a_users_connectors_reaches_every_bot_that_held_them():
    """Reconnects tear down the live MCP session, so a proxy left registered in
    another bot's registry is a call into a dead session — or into a deleted
    connector's credentials — until that bot happens to sync again."""
    manager = ConnectorManager(store=None)
    chatting_bot = ToolRegistry()
    other_bot = ToolRegistry()
    state = _UserConnections()
    _register_scoped(chatting_bot, state, _StubTool("mcp_gmail_send", "proxy"), "gmail")
    _populate(other_bot, state)
    manager._users["u1"] = state

    await manager._close_user("u1", chatting_bot)

    assert not chatting_bot.is_registered("mcp_gmail_send")
    assert not other_bot.is_registered("mcp_gmail_send")
