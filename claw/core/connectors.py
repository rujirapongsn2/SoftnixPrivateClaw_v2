"""Per-user MCP connector manager.

Connects to the user's enabled MCP servers (stdio or streamable HTTP) and
registers their tools as `mcp_{connector}_{tool}` in the agent's registry.
Connections are cached per user and rebuilt only when the config changes.
"""

import asyncio
import shlex
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from loguru import logger
from mcp.shared.exceptions import McpError

from claw.db.stores import ConnectorStore
from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry

# Env keys with this prefix are turned into HTTP headers for remote (http)
# connectors instead of being passed as process env — this is how remote MCP
# endpoints (Composio, Softnix ONE, …) carry their bearer/api-key auth.
_HEADER_ENV_PREFIX = "HEADER_"

# Defaults, overridable per-instance via ConnectorSettings (claw/config.py) —
# module constants only so direct construction (tests, scratch scripts)
# doesn't need a full Settings object.
#
# Per-connector connect+list_tools budget. sync_tools() holds a per-user lock
# for its whole duration, so without a timeout a single misbehaving MCP server
# (e.g. one that hangs mid-handshake instead of raising) can block that lock
# forever — every subsequent /connectors listing or chat turn for that user
# then hangs too, indefinitely, until the process is restarted. A caught
# exception (bad auth, connection refused, ...) already surfaces in seconds
# via the try/except below; this timeout only bounds the hang case.
_CONNECT_TIMEOUT_SECONDS = 20

# Per-tool-call budget, same rationale as _CONNECT_TIMEOUT_SECONDS but for an
# already-connected session: a remote MCP server that hangs instead of
# responding (e.g. one whose malformed error response the client library
# can't parse and then never returns from) would otherwise leave the whole
# chat turn spinning forever with no way to recover short of restarting the
# process. ToolRegistry.execute() already wraps every tool call in a generic
# try/except and turns any exception into a normal "Error executing ..."
# result the model sees, so a timeout here surfaces exactly like any other
# tool failure — no special-casing needed upstream. Uses the mcp SDK's own
# per-request timeout (ClientSession.call_tool's read_timeout_seconds) rather
# than an external asyncio.wait_for: it's scoped with anyio.fail_after inside
# the same request/task, so cleanup of the SDK's own response-stream
# bookkeeping is guaranteed via its `finally` regardless of the timeout,
# instead of abandoning a separately-spawned wait_for task. Note this still
# only stops the CLIENT from waiting — it does not send the MCP protocol's
# notifications/cancelled to the remote server, so a slow-but-eventually-
# completing call can still finish (and act on) its side effect server-side
# after the client has already reported the call as failed.
_TOOL_CALL_TIMEOUT_SECONDS = 60


class McpToolProxy(Tool):
    def __init__(
        self,
        session: Any,
        connector: str,
        tool_name: str,
        description: str,
        schema: dict,
        *,
        tool_call_timeout_seconds: float = _TOOL_CALL_TIMEOUT_SECONDS,
    ):
        self._session = session
        self._remote_name = tool_name
        self._tool_call_timeout_seconds = tool_call_timeout_seconds
        self.name = f"mcp_{connector}_{tool_name}"
        self.description = f"[{connector}] {description or tool_name}"
        self.parameters = schema or {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            result = await self._session.call_tool(
                self._remote_name, kwargs, read_timeout_seconds=timedelta(seconds=self._tool_call_timeout_seconds)
            )
        except McpError as exc:
            if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
                return (
                    f"Error: {self.name} timed out after {self._tool_call_timeout_seconds}s "
                    "waiting for a response"
                )
            raise
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
    def __init__(
        self,
        store: ConnectorStore,
        connect_timeout_seconds: float = _CONNECT_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: float = _TOOL_CALL_TIMEOUT_SECONDS,
    ):
        self.store = store
        self.connect_timeout_seconds = connect_timeout_seconds
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self._users: dict[str, _UserConnections] = {}
        # Serialize sync_tools per user: it is now driven both by chat turns and
        # by the connectors listing endpoint (composer menu), which can overlap.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def status(self, user_id: str) -> dict[str, dict]:
        return dict(self._users.get(user_id, _UserConnections()).statuses)

    async def sync_tools(self, user_id: str, registry: ToolRegistry) -> None:
        """Ensure the registry reflects the user's enabled connectors. Cheap
        when unchanged. A connector that ended in "error" last time
        (including a connect timeout) is always retried here even though its
        config didn't change — otherwise a transient failure would cache as
        permanently broken until an admin edits the connector, which defeats
        the point of timing failures out instead of hanging."""
        async with self._lock(user_id):
            connectors = await self.store.enabled_for_user(user_id)
            signature = tuple(sorted((c.id, c.updated_at.isoformat()) for c in connectors))
            state = self._users.get(user_id)
            had_error = state is not None and any(s.get("status") == "error" for s in state.statuses.values())
            if state is not None and state.signature == signature and not had_error:
                return

            await self._close_user(user_id, registry)
            state = _UserConnections(signature=signature, stack=AsyncExitStack())
            self._users[user_id] = state
            if not connectors:
                return

            await state.stack.__aenter__()
            # Connect all of a user's connectors concurrently, not one at a
            # time — otherwise N broken/hanging connectors cost N times the
            # per-connector timeout instead of one timeout period total,
            # while this method holds the per-user lock throughout.
            results = await asyncio.gather(*(self._connect_one(state.stack, c) for c in connectors))
            for connector, session, listed, error in results:
                if error is not None:
                    state.statuses[connector.name] = error
                    logger.warning("MCP connector {} {}", connector.name, error["error"])
                    continue
                count = 0
                for tool in listed.tools:
                    proxy = McpToolProxy(
                        session,
                        connector.name,
                        tool.name,
                        tool.description or "",
                        tool.inputSchema or {},
                        tool_call_timeout_seconds=self.tool_call_timeout_seconds,
                    )
                    registry.register(proxy)
                    state.tool_names.append(proxy.name)
                    count += 1
                state.statuses[connector.name] = {"status": "connected", "tools": count}
                logger.info("MCP connector {} connected with {} tools", connector.name, count)

    async def _connect_one(self, stack: AsyncExitStack, connector) -> tuple[Any, Any, Any, dict | None]:
        """Connect+list one connector under its own timeout. Never raises —
        returns (connector, session, listed, error_status), so callers can
        run several of these concurrently (via asyncio.gather) and still
        tell which ones failed."""
        try:
            session, listed = await asyncio.wait_for(
                self._connect_and_list(stack, connector),
                timeout=self.connect_timeout_seconds,
            )
            return connector, session, listed, None
        except TimeoutError:
            message = f"timed out after {self.connect_timeout_seconds}s connecting/listing tools"
            return connector, None, None, {"status": "error", "error": message}
        except Exception as exc:
            return connector, None, None, {"status": "error", "error": str(exc)[:300]}

    async def _connect_and_list(self, stack: AsyncExitStack, connector) -> tuple[Any, Any]:
        session = await self._connect(stack, connector)
        listed = await session.list_tools()
        return session, listed

    async def _connect(self, stack: AsyncExitStack, connector) -> Any:
        from mcp import ClientSession

        if connector.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            # Split env into HTTP headers (HEADER_*) and everything else.
            headers = {
                key[len(_HEADER_ENV_PREFIX):]: value
                for key, value in (connector.env or {}).items()
                if key.startswith(_HEADER_ENV_PREFIX) and value
            }
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(connector.url, headers=headers or None)
            )
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            argv = shlex.split(connector.command)
            if not argv:
                raise ValueError("empty command")
            # Built-in preset servers launch as `python -m claw.integrations.*`.
            # Use the running interpreter so the `claw` package is importable
            # regardless of what `python`/`python3` resolves to on PATH.
            if argv[0] in ("python", "python3"):
                argv[0] = sys.executable
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
