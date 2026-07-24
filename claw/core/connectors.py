"""Per-user MCP connector manager.

Connects to the user's enabled MCP servers (stdio or streamable HTTP) and
registers their tools as `mcp_{connector}_{tool}` in the agent's registry.
Connections are cached per user and rebuilt only when the config changes.
"""

import asyncio
import shlex
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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

# Same idea for endpoints that expect auth as a URL query parameter instead of
# a header (e.g. Alpha Vantage's `?apikey=`) — kept out of the stored `url`
# itself so the secret stays in the encrypted `env` column, not the plaintext
# url column, and is appended to the request URL only at connect time.
_QUERY_ENV_PREFIX = "QUERY_"

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

# After a connector fails, hold off retrying the whole set for this long — see
# ConnectorSettings.error_retry_cooldown_seconds and sync_tools() for why. A
# module constant so direct construction (tests) has a sane default.
_ERROR_RETRY_COOLDOWN_SECONDS = 60

# Bounds for a connector's own `timeout_ms` override (Settings > Connectors'
# "Timeout (ms)" field) — mirrors the range enforced by ConnectorBody in
# claw/api/manage.py, re-checked here in case anything else ever writes
# timeout_ms directly (defense in depth, not the primary validation).
_MIN_TIMEOUT_MS = 1000
_MAX_TIMEOUT_MS = 120_000


def _redact_secrets(text: str, connector) -> str:
    """Strip a connector's secret env values out of a freeform error string
    before it's logged or returned via the API. For an http connector using
    QUERY_* auth, the secret is embedded in the request URL itself, so an
    underlying httpx/mcp exception's message (e.g. on a bad/rate-limited key)
    includes the raw URL, secret and all — this scrubs any such value
    (header or query) wherever it appears in the message, not just the URL."""
    for value in (connector.env or {}).values():
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text


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
    # time.monotonic() of the most recent sync that left at least one connector
    # in "error"; drives the retry cooldown in sync_tools. Monotonic (not
    # wall-clock) so an NTP/clock adjustment can't skew the cooldown window.
    # None once a sync ends fully healthy.
    errored_monotonic: float | None = None


class ConnectorManager:
    def __init__(
        self,
        store: ConnectorStore,
        connect_timeout_seconds: float = _CONNECT_TIMEOUT_SECONDS,
        tool_call_timeout_seconds: float = _TOOL_CALL_TIMEOUT_SECONDS,
        error_retry_cooldown_seconds: float = _ERROR_RETRY_COOLDOWN_SECONDS,
    ):
        self.store = store
        self.connect_timeout_seconds = connect_timeout_seconds
        self.tool_call_timeout_seconds = tool_call_timeout_seconds
        self.error_retry_cooldown_seconds = error_retry_cooldown_seconds
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

    def _effective_timeout_seconds(self, connector, default_seconds: float) -> float:
        """A connector's own `timeout_ms` (if set) overrides the instance-wide
        connect/tool-call default, for both budgets — the UI exposes a single
        "Timeout (ms)" field per connector rather than separate connect vs.
        tool-call knobs."""
        raw = getattr(connector, "timeout_ms", None)
        if raw is None:
            return default_seconds
        return max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, raw)) / 1000

    async def status(self, user_id: str) -> dict[str, dict]:
        return dict(self._users.get(user_id, _UserConnections()).statuses)

    async def resolve_tool_names(self, user_id: str, connector_id: str) -> list[str] | None:
        """The tool names a connector is CURRENTLY registered under
        (`mcp_{connector.name}_{tool}`), looked up by the connector's stable
        id rather than its (renameable) name — so a skill linked to this id
        stays correct across a rename or delete+recreate. Requires
        sync_tools() to have already run for this user in this process
        (i.e. call this after sync_tools, not before). None if the connector
        doesn't belong to this user, or isn't currently connected."""
        connectors = await self.store.list_for_user(user_id)
        connector = next((c for c in connectors if c.id == connector_id), None)
        if connector is None:
            return None
        state = self._users.get(user_id)
        if state is None:
            return None
        status = state.statuses.get(connector.name)
        if status is None or status.get("status") != "connected":
            return None
        return status.get("tool_names")

    async def sync_tools(self, user_id: str, registry: ToolRegistry) -> None:
        """Ensure the registry reflects the user's enabled connectors. Cheap
        when unchanged. A connector that ended in "error" last time (including
        a connect timeout) is retried here even though its config didn't change
        — otherwise a transient failure would cache as permanently broken until
        an admin edits the connector. But that retry tears down and reconnects
        ALL of the user's connectors, waiting the full connect timeout for the
        broken one — up to a connector's own `timeout_ms` override if it has
        one (see `_effective_timeout_seconds`), which can be as long as
        `_MAX_TIMEOUT_MS` (120s), not just the shorter instance-wide default —
        and this method runs on every chat turn and every /connectors
        listing. So the retry is held off for
        `error_retry_cooldown_seconds` after the failure: within that window we
        short-circuit on the cached (error) state, exactly as for an unchanged
        all-healthy config, keeping turns and page loads fast. A config change
        (signature) or an explicit invalidate() still forces an immediate
        rebuild regardless of the cooldown."""
        async with self._lock(user_id):
            connectors = await self.store.enabled_for_user(user_id)
            signature = tuple(sorted((c.id, c.updated_at.isoformat()) for c in connectors))
            state = self._users.get(user_id)
            had_error = state is not None and any(s.get("status") == "error" for s in state.statuses.values())
            within_error_cooldown = (
                had_error
                and state.errored_monotonic is not None
                and (time.monotonic() - state.errored_monotonic) < self.error_retry_cooldown_seconds
            )
            if state is not None and state.signature == signature and (not had_error or within_error_cooldown):
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
                registered_names: list[str] = []
                for tool in listed.tools:
                    proxy = McpToolProxy(
                        session,
                        connector.name,
                        tool.name,
                        tool.description or "",
                        tool.inputSchema or {},
                        tool_call_timeout_seconds=self._effective_timeout_seconds(
                            connector, self.tool_call_timeout_seconds
                        ),
                    )
                    registry.register(proxy)
                    state.tool_names.append(proxy.name)
                    registered_names.append(proxy.name)
                # `tool_names` here are the exact names a skill's instructions
                # must reference to call this connector's tools (the
                # `mcp_{connector}_{tool}` prefix, not the server's raw tool
                # name) — surfaced in the Connectors UI so skill authors don't
                # have to guess it.
                state.statuses[connector.name] = {
                    "status": "connected",
                    "tools": len(registered_names),
                    "tool_names": registered_names,
                }
                logger.info("MCP connector {} connected with {} tools", connector.name, len(registered_names))
            # Stamp the failure time so the next sync holds off retrying the
            # whole set until the cooldown elapses (see sync_tools docstring).
            if any(s.get("status") == "error" for s in state.statuses.values()):
                state.errored_monotonic = time.monotonic()

    async def _connect_one(self, stack: AsyncExitStack, connector) -> tuple[Any, Any, Any, dict | None]:
        """Connect+list one connector under its own timeout. Never raises —
        returns (connector, session, listed, error_status), so callers can
        run several of these concurrently (via asyncio.gather) and still
        tell which ones failed."""
        timeout = self._effective_timeout_seconds(connector, self.connect_timeout_seconds)
        try:
            session, listed = await asyncio.wait_for(
                self._connect_and_list(stack, connector),
                timeout=timeout,
            )
            return connector, session, listed, None
        except TimeoutError:
            message = f"timed out after {timeout}s connecting/listing tools"
            return connector, None, None, {"status": "error", "error": message}
        except asyncio.CancelledError:
            # A broken connector's handshake fails deep inside the MCP SDK's
            # anyio internals (e.g. a DNS error becomes "Attempted to exit
            # cancel scope in a different task than it was entered in" because
            # the client's contexts are held across tasks in a shared stack),
            # which surfaces as a CancelledError on THIS gather-spawned child
            # task — with our own wait_for timeout as another source.
            # CancelledError is a BaseException, so the `except Exception`
            # below misses it; left unhandled it escapes gather() →
            # sync_tools() → warm_connectors()'s `except Exception` and
            # surfaces as a 500 on the /connectors listing (and would abort a
            # chat turn). uncancel() clears this child task's spurious
            # cancellation so it doesn't leak, and we report the connector as
            # errored. A genuine cancellation of the PARENT sync task is
            # unaffected: it is awaiting gather(), so its own CancelledError
            # still fires there regardless of what this child returns.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            message = f"failed connecting (within {timeout}s budget)"
            return connector, None, None, {"status": "error", "error": message}
        except Exception as exc:
            message = _redact_secrets(str(exc), connector)[:300]
            return connector, None, None, {"status": "error", "error": message}

    async def _connect_and_list(self, stack: AsyncExitStack, connector) -> tuple[Any, Any]:
        session = await self._connect(stack, connector)
        listed = await session.list_tools()
        return session, listed

    async def _connect(self, stack: AsyncExitStack, connector) -> Any:
        from mcp import ClientSession

        if connector.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            # Split env into HTTP headers (HEADER_*), URL query params
            # (QUERY_*), and everything else.
            headers = {
                key[len(_HEADER_ENV_PREFIX):]: value
                for key, value in (connector.env or {}).items()
                if key.startswith(_HEADER_ENV_PREFIX) and value
            }
            # An empty string is a deliberate "clear this param" override, not
            # "not set" — distinct from the key being absent entirely — so it
            # isn't filtered out here the way headers are; it's handled below.
            query_overrides = {
                key[len(_QUERY_ENV_PREFIX):]: value
                for key, value in (connector.env or {}).items()
                if key.startswith(_QUERY_ENV_PREFIX)
            }
            url = connector.url
            if query_overrides:
                parts = urlsplit(url)
                # Rebuild from the parsed pairs (not a dict) so a duplicate
                # query key already in the stored URL that no override
                # touches is preserved as-is instead of being collapsed to
                # its last value.
                existing = parse_qsl(parts.query, keep_blank_values=True)
                merged = [(k, v) for k, v in existing if k not in query_overrides]
                merged.extend((k, v) for k, v in query_overrides.items() if v)
                url = urlunsplit(parts._replace(query=urlencode(merged)))
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers or None)
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
            except asyncio.CancelledError:
                # Same cross-task anyio hazard as _connect_one: tearing down a
                # (possibly half-entered) MCP client context that was entered
                # on a different task can surface as a CancelledError — a
                # BaseException the `except Exception` below would miss, so it
                # would escape _close_user → sync_tools → the /connectors
                # endpoint as a 500. Absorb this task's spurious cancellation
                # (uncancel) and move on; a genuine cancellation of the caller
                # still fires at its own next await. See _connect_one.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                logger.debug("MCP stack close for {} cancelled across tasks; ignored", user_id)
            except Exception:
                # MCP SDK cancel-scope cleanup can be noisy across tasks; harmless.
                logger.debug("MCP stack close for {} raised; ignored", user_id)

    async def invalidate(self, user_id: str) -> None:
        """Force reconnect on next sync (config changed)."""
        state = self._users.get(user_id)
        if state is not None:
            state.signature = ()
