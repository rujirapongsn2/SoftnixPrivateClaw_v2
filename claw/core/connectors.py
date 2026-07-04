"""Per-user MCP connector manager.

Connects to the user's enabled MCP servers (stdio or streamable HTTP) and
registers their tools as `mcp_{connector}_{tool}` in the agent's registry.
Connections are cached per user and rebuilt only when the config changes.
"""

import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from claw.db.stores import ConnectorStore
from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry


class McpToolProxy(Tool):
    def __init__(self, session: Any, connector: str, tool_name: str, description: str, schema: dict):
        self._session = session
        self._remote_name = tool_name
        self.name = f"mcp_{connector}_{tool_name}"
        self.description = f"[{connector}] {description or tool_name}"
        self.parameters = schema or {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        result = await self._session.call_tool(self._remote_name, kwargs)
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        if getattr(result, "isError", False):
            return "Error: " + ("\n".join(parts) or "MCP tool call failed")
        return "\n".join(parts) or "(empty result)"


@dataclass
class _UserConnections:
    signature: tuple = ()
    stack: AsyncExitStack | None = None
    tool_names: list[str] = field(default_factory=list)
    statuses: dict[str, dict] = field(default_factory=dict)


class ConnectorManager:
    def __init__(self, store: ConnectorStore):
        self.store = store
        self._users: dict[str, _UserConnections] = {}

    async def status(self, user_id: str) -> dict[str, dict]:
        return dict(self._users.get(user_id, _UserConnections()).statuses)

    async def sync_tools(self, user_id: str, registry: ToolRegistry) -> None:
        """Ensure the registry reflects the user's enabled connectors. Cheap when unchanged."""
        connectors = await self.store.enabled_for_user(user_id)
        signature = tuple(sorted((c.id, c.updated_at.isoformat()) for c in connectors))
        state = self._users.get(user_id)
        if state is not None and state.signature == signature:
            return

        await self._close_user(user_id, registry)
        state = _UserConnections(signature=signature, stack=AsyncExitStack())
        self._users[user_id] = state
        if not connectors:
            return

        await state.stack.__aenter__()
        for connector in connectors:
            try:
                session = await self._connect(state.stack, connector)
                listed = await session.list_tools()
                count = 0
                for tool in listed.tools:
                    proxy = McpToolProxy(
                        session,
                        connector.name,
                        tool.name,
                        tool.description or "",
                        tool.inputSchema or {},
                    )
                    registry.register(proxy)
                    state.tool_names.append(proxy.name)
                    count += 1
                state.statuses[connector.name] = {"status": "connected", "tools": count}
                logger.info("MCP connector {} connected with {} tools", connector.name, count)
            except Exception as exc:
                state.statuses[connector.name] = {"status": "error", "error": str(exc)[:300]}
                logger.warning("MCP connector {} failed: {}", connector.name, exc)

    async def _connect(self, stack: AsyncExitStack, connector) -> Any:
        from mcp import ClientSession

        if connector.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await stack.enter_async_context(streamablehttp_client(connector.url))
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            argv = shlex.split(connector.command)
            if not argv:
                raise ValueError("empty command")
            params = StdioServerParameters(
                command=argv[0], args=argv[1:], env={**(connector.env or {})} or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _close_user(self, user_id: str, registry: ToolRegistry) -> None:
        state = self._users.pop(user_id, None)
        if state is None:
            return
        for name in state.tool_names:
            registry.unregister(name)
        if state.stack is not None:
            try:
                await state.stack.aclose()
            except Exception:
                # MCP SDK cancel-scope cleanup can be noisy across tasks; harmless.
                logger.debug("MCP stack close for {} raised; ignored", user_id)

    async def invalidate(self, user_id: str) -> None:
        """Force reconnect on next sync (config changed)."""
        state = self._users.get(user_id)
        if state is not None:
            state.signature = ()
