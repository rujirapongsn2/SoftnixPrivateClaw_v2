"""ConnectorManager: a single connector that hangs mid-handshake or on a tool
call must not block the per-user lock (or a chat turn) forever — every
subsequent /connectors listing or chat turn for that user would otherwise
hang indefinitely too, until the process is restarted (the actual incident
these tests guard against). Also covers that a previously-errored connector
is retried on the next sync rather than staying cached as permanently broken."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from sqlalchemy import update

from claw.core.connectors import ConnectorManager, McpToolProxy
from claw.db.models import McpConnector
from claw.db.stores import ConnectorStore, UserStore
from claw.tools.registry import ToolRegistry


async def test_hanging_connector_times_out_instead_of_blocking_forever(db_factory, monkeypatch):
    users = UserStore(db_factory)
    user = await users.create(email="hang@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "stuck", transport="http", url="https://example.invalid/mcp", enabled=True)

    async def hangs_forever(self, stack, connector):
        await asyncio.sleep(10)  # far longer than the timeout below
        raise AssertionError("should have been cancelled by the timeout")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", hangs_forever)

    mgr = ConnectorManager(store, connect_timeout_seconds=0.05)
    registry = ToolRegistry()

    await asyncio.wait_for(mgr.sync_tools(user.id, registry), timeout=2)

    status = await mgr.status(user.id)
    assert status["stuck"]["status"] == "error"
    assert "timed out after 0.05s" in status["stuck"]["error"]


async def test_cancel_scope_error_becomes_connector_error_not_a_500(db_factory, monkeypatch):
    """The MCP SDK's anyio internals can raise CancelledError ("Cancelled via
    cancel scope") from a broken connector's handshake. CancelledError is a
    BaseException, so it bypasses the generic `except Exception` and, left
    unhandled, would escape sync_tools → the /connectors endpoint as a 500
    (the intermittent failure this guards against). _connect_one must turn it
    into a normal connector error instead — as long as the surrounding task
    isn't itself being cancelled."""
    users = UserStore(db_factory)
    user = await users.create(email="cancelscope@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "scopey", transport="http", url="https://example.invalid/mcp", enabled=True)

    async def raises_cancel_scope(self, stack, connector):
        # Mimic anyio raising a bare CancelledError from inside the handshake.
        raise asyncio.CancelledError("Cancelled via cancel scope 0xdeadbeef")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", raises_cancel_scope)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()

    # Must NOT raise (no 500); the connector is simply reported as errored.
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["scopey"]["status"] == "error"


async def test_genuine_task_cancellation_still_propagates(db_factory, monkeypatch):
    """The CancelledError catch above must not swallow a genuine cancellation
    of the surrounding task (shutdown/drain, client hangup) — otherwise
    cooperative cancellation breaks. When the sync task itself is cancelled,
    sync_tools must still raise CancelledError."""
    users = UserStore(db_factory)
    user = await users.create(email="realcancel@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "slow", transport="http", url="https://example.invalid/mcp", enabled=True)

    started = asyncio.Event()

    async def blocks(self, stack, connector):
        started.set()
        await asyncio.sleep(30)  # will be interrupted by the outer cancel

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", blocks)

    mgr = ConnectorManager(store, connect_timeout_seconds=30)
    registry = ToolRegistry()

    task = asyncio.ensure_future(mgr.sync_tools(user.id, registry))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_second_connector_still_syncs_after_first_one_times_out(db_factory, monkeypatch):
    users = UserStore(db_factory)
    user = await users.create(email="partial@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "broken", transport="http", url="https://example.invalid/mcp", enabled=True)
    await store.upsert(user.id, "working", transport="http", url="https://example.invalid/mcp", enabled=True)

    class FakeSession:
        async def list_tools(self):
            class Listed:
                tools = []

            return Listed()

    async def fake_connect_and_list(self, stack, connector):
        if connector.name == "broken":
            await asyncio.sleep(10)
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store, connect_timeout_seconds=0.05)
    registry = ToolRegistry()

    await asyncio.wait_for(mgr.sync_tools(user.id, registry), timeout=2)

    status = await mgr.status(user.id)
    assert status["broken"]["status"] == "error"
    assert status["working"]["status"] == "connected"


async def test_connectors_connect_concurrently_not_sequentially(db_factory, monkeypatch):
    """N slow-but-working connectors must all connect in parallel — total
    wait bounded by the slowest one, not by their sum — since sync_tools
    holds the per-user lock for its whole duration."""
    users = UserStore(db_factory)
    user = await users.create(email="concurrent@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    names = ["a", "b", "c"]
    for name in names:
        await store.upsert(user.id, name, transport="http", url="https://example.invalid/mcp", enabled=True)

    class FakeSession:
        async def list_tools(self):
            class Listed:
                tools = []

            return Listed()

    async def slow_connect_and_list(self, stack, connector):
        await asyncio.sleep(0.2)
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", slow_connect_and_list)

    mgr = ConnectorManager(store, connect_timeout_seconds=5)
    registry = ToolRegistry()

    loop = asyncio.get_event_loop()
    start = loop.time()
    await mgr.sync_tools(user.id, registry)
    elapsed = loop.time() - start

    # Sequential would take >= 0.6s (3 * 0.2s); concurrent stays near 0.2s.
    assert elapsed < 0.4
    status = await mgr.status(user.id)
    assert all(status[name]["status"] == "connected" for name in names)


class _FlakyConnect:
    """Fails the first N connect attempts, then succeeds. Tracks attempt count.

    Patched onto ConnectorManager._connect_and_list as an instance (not a
    function), so it is NOT bound as a method — hence no manager `self`
    parameter here; sync_tools calls it as `self._connect_and_list(stack, c)`."""

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.attempts = 0

    async def __call__(self, stack, connector):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("temporary failure")

        class Listed:
            tools = []

        class FakeSession:
            async def list_tools(self):
                return Listed()

        session = FakeSession()
        return session, await session.list_tools()


async def test_errored_connector_is_retried_on_next_sync_without_config_change(db_factory, monkeypatch):
    """A connector that timed out must not stay cached as permanently
    broken — it has to be retried the next time sync_tools runs even though
    nothing in its DB row changed. cooldown=0 is the retry-on-every-sync
    behavior (the cooldown-disabled case)."""
    users = UserStore(db_factory)
    user = await users.create(email="retry@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "flaky", transport="http", url="https://example.invalid/mcp", enabled=True)

    flaky = _FlakyConnect(fail_times=1)
    monkeypatch.setattr(ConnectorManager, "_connect_and_list", flaky)

    mgr = ConnectorManager(store, error_retry_cooldown_seconds=0)
    registry = ToolRegistry()

    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "error"

    # No DB change at all — same signature — yet with cooldown=0 the connector
    # is retried instead of the cache short-circuiting sync_tools.
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "connected"
    assert flaky.attempts == 2


async def test_errored_connector_not_retried_within_cooldown(db_factory, monkeypatch):
    """With a cooldown in effect, a just-failed connector is NOT reconnected on
    the next sync — the whole set short-circuits on the cached error state, so
    a broken connector can't add the connect timeout to every chat turn and
    every /connectors listing."""
    users = UserStore(db_factory)
    user = await users.create(email="cooldown@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "flaky", transport="http", url="https://example.invalid/mcp", enabled=True)

    flaky = _FlakyConnect(fail_times=1)
    monkeypatch.setattr(ConnectorManager, "_connect_and_list", flaky)

    mgr = ConnectorManager(store, error_retry_cooldown_seconds=60)
    registry = ToolRegistry()

    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "error"
    assert flaky.attempts == 1

    # Immediately syncing again is within the cooldown → no reconnect attempt.
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "error"
    assert flaky.attempts == 1


async def test_errored_connector_retried_after_cooldown_elapses(db_factory, monkeypatch):
    """Once the cooldown window has passed, the next sync retries — proven
    deterministically by backdating the recorded failure time rather than
    sleeping."""
    users = UserStore(db_factory)
    user = await users.create(email="cooldown2@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "flaky", transport="http", url="https://example.invalid/mcp", enabled=True)

    flaky = _FlakyConnect(fail_times=1)
    monkeypatch.setattr(ConnectorManager, "_connect_and_list", flaky)

    mgr = ConnectorManager(store, error_retry_cooldown_seconds=60)
    registry = ToolRegistry()

    await mgr.sync_tools(user.id, registry)
    assert flaky.attempts == 1

    # Pretend the failure happened well before the cooldown window.
    mgr._users[user.id].errored_at -= timedelta(seconds=120)

    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "connected"
    assert flaky.attempts == 2


async def test_invalidate_overrides_error_cooldown(db_factory, monkeypatch):
    """A config change (invalidate) forces an immediate retry even inside the
    cooldown window — so fixing a broken connector takes effect right away
    instead of waiting out the cooldown."""
    users = UserStore(db_factory)
    user = await users.create(email="cooldown3@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "flaky", transport="http", url="https://example.invalid/mcp", enabled=True)

    flaky = _FlakyConnect(fail_times=1)
    monkeypatch.setattr(ConnectorManager, "_connect_and_list", flaky)

    mgr = ConnectorManager(store, error_retry_cooldown_seconds=60)
    registry = ToolRegistry()

    await mgr.sync_tools(user.id, registry)
    assert flaky.attempts == 1

    # invalidate() (called by the connector upsert/delete endpoints) must
    # bypass the cooldown.
    await mgr.invalidate(user.id)
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["flaky"]["status"] == "connected"
    assert flaky.attempts == 2


async def test_tool_call_times_out_instead_of_hanging_the_turn_forever():
    """A connected session can still hang/error on an individual call_tool
    (e.g. the remote server sends a malformed response) — this must surface
    as a normal tool error, not spin the chat turn forever. McpToolProxy
    relies on the mcp SDK's own per-call read_timeout_seconds (passed
    through to session.call_tool) rather than wrapping externally, so the
    fake session here raises the same McpError the real SDK raises on its
    internal timeout."""

    class TimingOutSession:
        async def call_tool(self, name, kwargs, read_timeout_seconds=None):
            assert read_timeout_seconds == timedelta(seconds=0.05)
            raise McpError(ErrorData(code=httpx.codes.REQUEST_TIMEOUT, message="Timed out"))

    proxy = McpToolProxy(
        TimingOutSession(), "softnixkb", "search_knowledge", "desc", {}, tool_call_timeout_seconds=0.05
    )
    result = await proxy.execute(query="x")

    assert result.startswith("Error:")
    assert "timed out after 0.05s" in result


async def test_tool_call_non_timeout_mcp_error_is_not_swallowed():
    """A non-timeout McpError (e.g. the remote tool genuinely rejected the
    call) must propagate — ToolRegistry.execute() already turns any raised
    exception into a normal "Error executing ..." result, so McpToolProxy
    must not silently absorb errors that aren't its own timeout."""

    class RejectingSession:
        async def call_tool(self, name, kwargs, read_timeout_seconds=None):
            raise McpError(ErrorData(code=403, message="not allowed"))

    proxy = McpToolProxy(RejectingSession(), "softnixkb", "search_knowledge", "desc", {})
    with pytest.raises(McpError):
        await proxy.execute(query="x")


async def test_resolve_tool_names_survives_connector_rename(db_factory, monkeypatch):
    """A skill links to a connector by its stable id. Renaming the connector
    must not break that link — resolve_tool_names looks up the CURRENT name
    live, so the returned tool names always reflect the connector's present
    display name, not whatever it was called when the skill was written."""
    users = UserStore(db_factory)
    user = await users.create(email="rename@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    connector = await store.upsert(user.id, "softnixkb", transport="http", url="https://example.invalid/mcp", enabled=True)

    class FakeSession:
        async def list_tools(self):
            class Tool:
                name = "search_knowledge"
                description = "search"
                inputSchema = {}

            class Listed:
                tools = [Tool()]

            return Listed()

    async def fake_connect_and_list(self, stack, c):
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    names = await mgr.resolve_tool_names(user.id, connector.id)
    assert names == ["mcp_softnixkb_search_knowledge"]

    # Rename the connector — same id, different name (the store's own upsert()
    # can't rename in place since `name` is its lookup key, not an update
    # field, so mutate the row directly to simulate it) — then force a re-sync.
    async with db_factory() as db:
        await db.execute(
            update(McpConnector).where(McpConnector.id == connector.id).values(name="softnix-kb-v2")
        )
        await db.commit()
    await mgr.invalidate(user.id)
    await mgr.sync_tools(user.id, registry)

    names = await mgr.resolve_tool_names(user.id, connector.id)
    assert names == ["mcp_softnix-kb-v2_search_knowledge"]


async def test_resolve_tool_names_none_for_unknown_or_disconnected(db_factory):
    users = UserStore(db_factory)
    user = await users.create(email="noconn@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    mgr = ConnectorManager(store)

    assert await mgr.resolve_tool_names(user.id, "nonexistent-id") is None


async def test_tool_call_returns_normally_when_within_the_timeout():
    class FakeContentItem:
        text = "the answer"

    class FakeResult:
        content = [FakeContentItem()]
        isError = False

    class FastSession:
        async def call_tool(self, name, kwargs, read_timeout_seconds=None):
            return FakeResult()

    proxy = McpToolProxy(FastSession(), "softnixkb", "search_knowledge", "desc", {})
    result = await proxy.execute(query="x")

    assert result == "the answer"
