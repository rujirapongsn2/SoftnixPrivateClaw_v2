"""ConnectorManager: a single connector that hangs mid-handshake or on a tool
call must not block the per-user lock (or a chat turn) forever — every
subsequent /connectors listing or chat turn for that user would otherwise
hang indefinitely too, until the process is restarted (the actual incident
these tests guard against). Also covers that a previously-errored connector
is retried on the next sync rather than staying cached as permanently broken."""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from sqlalchemy import update

from claw.core.connectors import ConnectorManager, McpToolProxy
from claw.db.models import McpConnector
from claw.db.stores import ConnectorStore, UserStore
from claw.security.ssrf import UnsafeUrlError
from claw.tools.registry import ToolRegistry


async def _resolves_public(url: str) -> list[str]:
    """Stand-in for claw.security.ssrf.resolve_public_ips so a test that walks
    the real _connect path doesn't depend on live DNS."""
    return ["93.184.216.34"]


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


async def test_teardown_cancel_scope_error_does_not_escape(db_factory, monkeypatch):
    """Symmetric to the connect path: tearing down a broken connector's
    half-entered context on a later rebuild can raise CancelledError from
    anyio's cross-task cancel-scope exit. _close_user must absorb it too —
    otherwise it escapes sync_tools → the /connectors endpoint as a 500 on the
    NEXT sync (config change / cooldown expiry), not just the first."""
    users = UserStore(db_factory)
    user = await users.create(email="teardown@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "kb", transport="http", url="https://example.invalid/mcp", enabled=True)

    class FakeSession:
        async def list_tools(self):
            class Listed:
                tools = []

            return Listed()

    async def ok_connect(self, stack, connector):
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", ok_connect)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    # Replace the live stack with one whose aclose raises a cross-task
    # CancelledError (what anyio surfaces when a context entered on another
    # task is exited here), then force a rebuild via invalidate().
    class CancellingStack:
        async def aclose(self):
            raise asyncio.CancelledError("Cancelled via cancel scope on teardown")

    mgr._users[user.id].stack = CancellingStack()
    await mgr.invalidate(user.id)

    # Must NOT raise — the teardown cancellation is absorbed and the rebuild
    # proceeds to reconnect.
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status(user.id))["kb"]["status"] == "connected"


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
    mgr._users[user.id].errored_monotonic -= 120

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


async def test_connect_error_redacts_query_secret_from_message(db_factory, monkeypatch):
    """A QUERY_* secret (e.g. Alpha Vantage's ?apikey=) is embedded in the
    request URL itself, so an underlying connect exception's message can
    contain it verbatim. That message is both logged (claw.log) and returned
    through GET /api/connectors' runtime.error field, so it must never
    reach either place with the raw secret still in it."""
    users = UserStore(db_factory)
    user = await users.create(email="leak@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id,
        "av",
        transport="http",
        url="https://mcp.alphavantage.co/mcp",
        env={"QUERY_apikey": "SUPERSECRETKEY123"},
        enabled=True,
    )

    async def raises_with_url_in_message(self, stack, connector):
        raise RuntimeError(
            "connect failed for https://mcp.alphavantage.co/mcp?apikey=SUPERSECRETKEY123"
        )

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", raises_with_url_in_message)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    status = await mgr.status(user.id)
    assert "SUPERSECRETKEY123" not in status["av"]["error"]
    assert "***" in status["av"]["error"]


async def test_query_env_preserves_duplicate_keys_and_clears_on_empty_string(db_factory, monkeypatch):
    """QUERY_* overrides must not collapse a duplicate query key already in
    the stored URL that no override touches, and an explicit empty-string
    QUERY_* value must clear/blank an existing param instead of being
    silently ignored and leaving the stale value in place."""
    users = UserStore(db_factory)
    user = await users.create(email="query@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    connector = await store.upsert(
        user.id,
        "av",
        transport="http",
        url="https://mcp.alphavantage.co/mcp?foo=1&foo=2&stale=old",
        env={"QUERY_apikey": "secret123", "QUERY_stale": ""},
        enabled=True,
    )

    captured: dict = {}

    class FakeReadWriteContext:
        async def __aenter__(self):
            return ("read", "write", None)

        async def __aexit__(self, *a):
            return False

    def fake_streamablehttp_client(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeReadWriteContext()

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamablehttp_client", fake_streamablehttp_client
    )
    monkeypatch.setattr("mcp.ClientSession", FakeSession)
    # _connect's SSRF guard would otherwise make this test depend on live DNS.
    monkeypatch.setattr("claw.core.connectors.resolve_public_ips", _resolves_public)

    mgr = ConnectorManager(store)
    async with AsyncExitStack() as stack:
        await mgr._connect(stack, connector)

    parts = urlsplit(captured["url"])
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    assert ("foo", "1") in pairs and ("foo", "2") in pairs
    assert not any(k == "stale" for k, _ in pairs)
    assert ("apikey", "secret123") in pairs


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


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.inputSchema = {}


class _FakeListed:
    def __init__(self, tool_names):
        self.tools = [_FakeTool(n) for n in tool_names]


async def test_global_connector_shared_across_users_one_connect_call(db_factory, monkeypatch):
    """A single admin-global connector (owner_id NULL) must be connected
    exactly once for the whole process — not once per user — and both users'
    registries get the shared tool without either configuring anything."""
    users = UserStore(db_factory)
    user_a = await users.create(email="global-a@x.io", password_hash="h")
    user_b = await users.create(email="global-b@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        None, "search", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    connect_calls = 0

    class FakeSession:
        async def list_tools(self):
            return _FakeListed(["web_search"])

    async def fake_connect_and_list(self, stack, connector):
        nonlocal connect_calls
        connect_calls += 1
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry_a = ToolRegistry()
    registry_b = ToolRegistry()

    await mgr.sync_tools(user_a.id, registry_a)
    await mgr.sync_tools(user_b.id, registry_b)

    assert connect_calls == 1
    assert registry_a.get("mcp_search_web_search") is not None
    assert registry_b.get("mcp_search_web_search") is not None
    assert (await mgr.status_global())["search"]["status"] == "connected"


async def test_users_own_connector_shadows_same_name_global_connector(db_factory, monkeypatch):
    """On a name collision between a global connector and a user's own private
    one, the user's own must win — mirroring LLMConfigStore.resolve()'s
    owner-vs-global tie-break — and the OTHER (non-colliding) user must still
    get the global one.

    Uses monkeypatched store methods rather than real overlapping-name rows:
    the partial-unique global-name index (postgresql_where) is Postgres-only
    — SQLAlchemy silently drops that clause on SQLite, so the index becomes a
    FULL unique constraint on `name` there, and the very row combination this
    test needs (a private + a global connector sharing a name) can't be
    inserted into the SQLite test DB, even though it's valid on Postgres in
    production. This is a pre-existing gap shared with LLMProvider's identical
    index shape, not something introduced here."""
    users = UserStore(db_factory)
    owner = await users.create(email="shadow-owner@x.io", password_hash="h")
    other = await users.create(email="shadow-other@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    now = datetime.now(timezone.utc)
    global_row = McpConnector(
        id="global1", owner_id=None, name="search", transport="http",
        url="https://global.invalid/mcp", enabled=True, updated_at=now,
    )
    private_row = McpConnector(
        id="private1", owner_id=owner.id, name="search", transport="http",
        url="https://private.invalid/mcp", enabled=True, updated_at=now,
    )

    async def fake_enabled_for_global(self):
        return [global_row]

    async def fake_enabled_for_user(self, owner_id):
        return [private_row] if owner_id == owner.id else []

    monkeypatch.setattr(ConnectorStore, "enabled_for_global", fake_enabled_for_global)
    monkeypatch.setattr(ConnectorStore, "enabled_for_user", fake_enabled_for_user)

    class FakeSession:
        pass

    async def fake_connect_and_list(self, stack, connector):
        session = FakeSession()
        # Distinguish the two "search" rows by which tool they expose.
        tool = "private_search" if connector.url == "https://private.invalid/mcp" else "web_search"
        return session, _FakeListed([tool])

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry_owner = ToolRegistry()
    registry_other = ToolRegistry()

    await mgr.sync_tools(owner.id, registry_owner)
    await mgr.sync_tools(other.id, registry_other)

    # Owner gets their OWN "search" connector's tool, not the global one.
    assert registry_owner.get("mcp_search_private_search") is not None
    assert registry_owner.get("mcp_search_web_search") is None
    # The other user, with no name collision, gets the global one.
    assert registry_other.get("mcp_search_web_search") is not None


async def test_resolve_tool_names_not_shadowed_by_a_disabled_own_connector(db_factory, monkeypatch):
    """A DISABLED private connector of the same name as a global one must NOT
    shadow the global connector for resolve_tool_names — sync_tools' own-vs-
    global tie-break only counts ENABLED own connectors (enabled_for_user), so
    a disabled same-name connector leaves the global one registered as normal.
    resolve_tool_names must agree, or a skill linked to that global connector
    would see its tools as unavailable (None) even though they're live in the
    registry — the exact mismatch this test guards against."""
    users = UserStore(db_factory)
    owner = await users.create(email="shadow-disabled@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    now = datetime.now(timezone.utc)
    global_row = McpConnector(
        id="global-disabled-shadow", owner_id=None, name="search", transport="http",
        url="https://global.invalid/mcp", enabled=True, updated_at=now,
    )
    disabled_private_row = McpConnector(
        id="private-disabled-shadow", owner_id=owner.id, name="search", transport="http",
        url="https://private.invalid/mcp", enabled=False, updated_at=now,
    )

    async def fake_enabled_for_global(self):
        return [global_row]

    async def fake_enabled_for_user(self, owner_id):
        return []  # the private "search" connector is disabled, so excluded

    async def fake_list_for_user(self, owner_id):
        return [disabled_private_row]  # but list_for_user returns it regardless

    async def fake_list_for_global(self):
        return [global_row]

    monkeypatch.setattr(ConnectorStore, "enabled_for_global", fake_enabled_for_global)
    monkeypatch.setattr(ConnectorStore, "enabled_for_user", fake_enabled_for_user)
    monkeypatch.setattr(ConnectorStore, "list_for_user", fake_list_for_user)
    monkeypatch.setattr(ConnectorStore, "list_for_global", fake_list_for_global)

    class FakeSession:
        pass

    async def fake_connect_and_list(self, stack, connector):
        return FakeSession(), _FakeListed(["web_search"])

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(owner.id, registry)

    # The global connector's tool IS actually registered for this user...
    assert registry.get("mcp_search_web_search") is not None
    # ...so resolve_tool_names must report it too, not None.
    assert await mgr.resolve_tool_names(owner.id, "global-disabled-shadow") == ["mcp_search_web_search"]


async def test_global_connector_invalidate_propagates_to_users_on_next_sync(db_factory, monkeypatch):
    """A global connector's config change (e.g. an admin editing its API key
    or URL via PUT /admin/connectors, which bumps updated_at and then calls
    invalidate_global()) must be picked up by every user's NEXT sync_tools —
    not stay stuck on a stale signature or, worse, a stale registered proxy
    bound to a now-closed session."""
    users = UserStore(db_factory)
    user = await users.create(email="global-invalidate@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        None, "search", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    tool_name = "web_search_v1"

    async def fake_connect_and_list(self, stack, connector):
        return FakeSession(), _FakeListed([tool_name])

    class FakeSession:
        pass

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)
    assert registry.get("mcp_search_web_search_v1") is not None

    tool_name = "web_search_v2"
    # Mirrors the admin PUT route: a real field change (bumps updated_at) then
    # invalidate_global(name) as a cooldown-bypass safety net, scoped to just
    # this one connector (see test below for why that scoping matters).
    await store.upsert(None, "search", url="https://example.invalid/mcp/v2", enabled=True)
    await mgr.invalidate_global("search")
    await mgr.sync_tools(user.id, registry)

    assert registry.get("mcp_search_web_search_v1") is None
    assert registry.get("mcp_search_web_search_v2") is not None


async def test_editing_one_global_connector_does_not_reconnect_others(db_factory, monkeypatch):
    """Editing (or fixing/removing) one admin-global connector must not tear
    down or reconnect any OTHER global connector's already-live session —
    otherwise every user of every OTHER pre-built connector would see a
    momentary disruption any time an admin touches an unrelated one. Counts
    actual connect attempts per connector name to prove "beta" is never
    reconnected when only "alpha"'s config changes."""
    store = ConnectorStore(db_factory)
    await store.upsert(None, "alpha", transport="http", url="https://a.invalid/mcp", enabled=True)
    await store.upsert(None, "beta", transport="http", url="https://b.invalid/mcp", enabled=True)

    connect_calls: list[str] = []

    async def fake_connect_and_list(self, stack, connector):
        connect_calls.append(connector.name)
        return _FakeSession(), _FakeListed([f"{connector.name}_tool"])

    class _FakeSession:
        pass

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    await mgr.sync_global()
    assert sorted(connect_calls) == ["alpha", "beta"]

    connect_calls.clear()
    # Admin edits only "alpha" (bumps updated_at) and invalidates just that one.
    await store.upsert(None, "alpha", url="https://a.invalid/mcp/v2", enabled=True)
    await mgr.invalidate_global("alpha")
    await mgr.sync_global()

    assert connect_calls == ["alpha"]
    statuses = await mgr.status_global()
    assert statuses["alpha"]["status"] == "connected"
    assert statuses["beta"]["status"] == "connected"


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


async def test_proxy_session_ref_follows_a_replaced_session_instead_of_going_stale():
    """A global connector's already-registered McpToolProxy must keep working
    after its session is torn down and replaced (admin edit, error-retry
    reconnect) — session_ref is looked up fresh on every call instead of
    using whatever session object was captured at construction time."""

    class FakeContentItem:
        text = "ok"

    class FakeResult:
        content = [FakeContentItem()]
        isError = False

    class FakeSession:
        async def call_tool(self, name, kwargs, read_timeout_seconds=None):
            return FakeResult()

    current: dict[str, object | None] = {"session": None}
    proxy = McpToolProxy(
        None, "search", "web_search", "desc", {}, session_ref=lambda: current["session"]
    )

    # No live session yet (e.g. between a teardown and its reconnect) — a
    # friendly error, not an AttributeError on None.
    result = await proxy.execute(query="x")
    assert "not currently connected" in result

    # The connector reconnects — the SAME proxy object (as if still
    # registered in some other user's registry from before the reconnect)
    # must now use the new session without being rebuilt or re-registered.
    current["session"] = FakeSession()
    result = await proxy.execute(query="x")
    assert result == "ok"


async def test_close_global_tears_down_immediately_without_waiting_for_sync(db_factory, monkeypatch):
    """Deleting a global connector must stop it being live/callable right
    away, not merely "eventually, on some future sync_global call that
    might itself be skipped under lock contention"."""
    store = ConnectorStore(db_factory)
    connector = await store.upsert(
        None, "search", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    class FakeSession:
        async def list_tools(self):
            return _FakeListed(["web_search"])

    async def fake_connect_and_list(self, stack, c):
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    await mgr.sync_global()
    assert (await mgr.status_global())["search"]["status"] == "connected"

    await mgr.close_global(connector.name, connector.id)

    assert (await mgr.status_global()) == {}
    assert mgr._global.sessions == {}
    assert mgr._global.proxies == {}


async def test_close_global_does_not_tear_down_a_recreated_connector_with_a_different_id(
    db_factory, monkeypatch
):
    """A close_global(name, id) call for a connector that was deleted must
    not tear down a DIFFERENT, newer connector that was later recreated
    under the same name — even though it's keyed by name, it must check the
    id it was meant for before closing anything."""
    store = ConnectorStore(db_factory)
    old = await store.upsert(
        None, "search", transport="http", url="https://old.invalid/mcp", enabled=True
    )

    class FakeSession:
        async def list_tools(self):
            return _FakeListed(["web_search"])

    async def fake_connect_and_list(self, stack, c):
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    await mgr.sync_global()

    # Simulate: "old" was deleted and a brand-new connector was recreated
    # under the same name (different id) before the original close_global
    # call for "old" got a chance to run.
    await store.delete(None, old.id)
    new = await store.upsert(
        None, "search", transport="http", url="https://new.invalid/mcp", enabled=True
    )
    await mgr.sync_global()
    assert (await mgr.status_global())["search"]["status"] == "connected"

    # The stale close_global for the OLD id must be a no-op now.
    await mgr.close_global("search", old.id)

    assert (await mgr.status_global())["search"]["status"] == "connected"
    assert mgr._global.signatures["search"][0] == new.id


async def test_close_global_gives_up_quickly_under_lock_contention(db_factory, monkeypatch):
    """close_global must not block an admin's delete request for as long as
    an unrelated, slow sync_global reconnect — it should give up quickly and
    let sync_global's own lazy cleanup handle it instead."""
    store = ConnectorStore(db_factory)
    connector = await store.upsert(
        None, "search", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_connect_and_list(self, stack, c):
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", slow_connect_and_list)

    mgr = ConnectorManager(store)
    holder = asyncio.ensure_future(mgr.sync_global())
    await started.wait()  # sync_global now holds _global_lock, mid-connect

    loop = asyncio.get_event_loop()
    before = loop.time()
    await mgr.close_global(connector.name, connector.id)
    elapsed = loop.time() - before

    assert elapsed < 2.0  # bounded by close_global's own 0.5s acquire timeout

    release.set()
    await holder


async def test_two_concurrent_first_syncs_both_see_the_result_instead_of_one_bailing_empty(
    db_factory, monkeypatch
):
    """Before the very first sync_global ever completes, a second caller
    racing the lock must wait for it rather than bailing out on a
    still-completely-empty pool (the lock-busy bypass is only safe once the
    pool has been populated at least once)."""
    store = ConnectorStore(db_factory)
    await store.upsert(
        None, "search", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    started = asyncio.Event()
    release = asyncio.Event()

    class FakeSession:
        async def list_tools(self):
            return _FakeListed(["web_search"])

    async def slow_connect_and_list(self, stack, c):
        started.set()
        await release.wait()
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", slow_connect_and_list)

    mgr = ConnectorManager(store)
    first = asyncio.ensure_future(mgr.sync_global())
    await started.wait()  # first call is now mid-connect, holding the lock

    second_done = asyncio.Event()

    async def run_second():
        await mgr.sync_global()
        second_done.set()

    second = asyncio.ensure_future(run_second())
    await asyncio.sleep(0.01)  # give second a chance to run and hit the lock check
    # Before the fix ("if locked: return" with no synced_once guard), second
    # would bail out and finish here, well before first's connect completes,
    # having contributed nothing to a still-completely-empty pool.
    assert not second_done.is_set()

    release.set()
    await first
    await second

    assert (await mgr.status_global())["search"]["status"] == "connected"


async def test_api_kind_connector_never_calls_mcp_connect(db_factory, monkeypatch):
    """A kind="api" connector must be built purely in-memory from its stored
    `operations` — sync_tools must never invoke _connect_one/_connect for it."""
    users = UserStore(db_factory)
    user = await users.create(email="apikind-sync@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id,
        "myapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        operations=[
            {
                "name": "get_thing",
                "method": "GET",
                "path": "/things/{id}",
                "description": "",
                "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
            }
        ],
        enabled=True,
    )

    async def raise_if_called(self, stack, connector):
        raise AssertionError("an api-kind connector must never reach _connect_one")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", raise_if_called)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    status = await mgr.status(user.id)
    assert status["myapi"]["status"] == "connected"
    assert status["myapi"]["tools"] == 1
    assert registry.get("api_myapi_get_thing") is not None


async def test_api_kind_and_mcp_connectors_coexist_in_sync_tools(db_factory, monkeypatch):
    """A mix of kind="api" and kind="mcp" connectors on the same user: the
    api one registers with zero I/O while the mcp one still goes through the
    normal connect/list flow, unaffected by the partitioning."""
    users = UserStore(db_factory)
    user = await users.create(email="mixedkind@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id,
        "myapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        operations=[
            {"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}
        ],
        enabled=True,
    )
    await store.upsert(user.id, "mymcp", transport="http", url="https://example.invalid/mcp", enabled=True)

    connect_calls: list[str] = []

    class FakeSession:
        async def list_tools(self):
            return _FakeListed(["search"])

    async def fake_connect_and_list(self, stack, connector):
        connect_calls.append(connector.name)
        session = FakeSession()
        return session, await session.list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    assert connect_calls == ["mymcp"]  # api-kind never went through this path
    status = await mgr.status(user.id)
    assert status["myapi"]["status"] == "connected"
    assert status["mymcp"]["status"] == "connected"
    assert registry.get("api_myapi_ping") is not None
    assert registry.get("mcp_mymcp_search") is not None


async def test_api_kind_signature_skip_still_works_when_unchanged(db_factory, monkeypatch):
    """The existing (id, updated_at) signature short-circuit must still avoid
    rebuilding an unchanged api-kind connector's tools on a second sync."""
    users = UserStore(db_factory)
    user = await users.create(email="apikind-skip@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id,
        "myapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        operations=[{"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}],
        enabled=True,
    )

    async def raise_if_called(self, stack, connector):
        raise AssertionError("must never be called for an api-kind connector")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", raise_if_called)

    mgr = ConnectorManager(store)
    registry_1 = ToolRegistry()
    await mgr.sync_tools(user.id, registry_1)
    assert registry_1.get("api_myapi_ping") is not None

    # Second sync with no config change: unchanged signature short-circuits
    # before even reaching the api/mcp partitioning logic.
    registry_2 = ToolRegistry()
    await mgr.sync_tools(user.id, registry_2)
    assert registry_2.get("api_myapi_ping") is None  # never re-registered into a fresh registry — skipped


async def test_api_kind_global_connector_never_calls_mcp_connect(db_factory, monkeypatch):
    """Mirrors the per-user test but for the admin-global shared pool
    (sync_global) — api-kind must skip the stack/session machinery entirely."""
    store = ConnectorStore(db_factory)
    await store.upsert(
        None,
        "globalapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        operations=[{"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}],
        enabled=True,
    )

    async def raise_if_called(self, stack, connector):
        raise AssertionError("an api-kind global connector must never reach _connect_one")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", raise_if_called)

    mgr = ConnectorManager(store)
    await mgr.sync_global()

    status = await mgr.status_global()
    assert status["globalapi"]["status"] == "connected"
    assert mgr._global.stacks == {}
    assert mgr._global.sessions == {}
    assert mgr._global.proxies["globalapi"][0].name == "api_globalapi_ping"


def test_default_tool_args_exempt_has_no_api_kind_glob():
    """A kind="api" connector is entirely user/admin-defined (unlike the
    curated communication connectors this default list exists for) — a
    blanket default exemption would silently bypass PII masking for
    connectors nobody has reviewed. An admin can still opt a specific one in
    at runtime via PolicyEngine's tool_args_exempt config."""
    from claw.security.policy import DEFAULT_TOOL_ARGS_EXEMPT

    assert not any(pattern.startswith("api_") for pattern in DEFAULT_TOOL_ARGS_EXEMPT)


async def test_admin_custom_exempt_entry_still_matches_api_tool_via_fnmatch(db_factory):
    """An admin-supplied custom tool_args_exempt entry (not a default) must
    still match an api-kind tool name via the existing fnmatch glob logic."""
    from claw.security.policy import PolicyEngine

    engine = PolicyEngine(tool_args_exempt=["api_myapi_*"])
    assert engine.is_tool_exempt("api_myapi_get_thing")
    assert not engine.is_tool_exempt("api_otherapi_get_thing")


async def test_malformed_api_operations_isolated_to_that_connector(db_factory):
    """A broken kind="api" connector must degrade exactly like a failed MCP
    connect: itself marked "error", every other connector unaffected. Without
    per-connector isolation, one malformed `operations` row raises straight out
    of sync_tools and takes down every connector for that user."""
    users = UserStore(db_factory)
    user = await users.create(email="badops@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    # Missing the "path" key entirely — what a bad manual DB edit or a partial
    # write leaves behind (the API layer's validation can't reach it).
    await store.upsert(
        user.id, "brokenapi", kind="api", transport="http", url="https://api.example.com",
        operations=[{"name": "oops", "method": "GET"}], enabled=True,
    )
    await store.upsert(
        user.id, "goodapi", kind="api", transport="http", url="https://ok.example.com",
        operations=[
            {"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}
        ],
        enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    status = await mgr.status(user.id)
    assert status["brokenapi"]["status"] == "error"
    assert status["goodapi"]["status"] == "connected"
    assert registry.get("api_goodapi_ping") is not None


async def test_disabling_a_global_api_connector_revokes_it_from_every_user(db_factory):
    """Disabling an admin-global kind="api" connector must actually stop it
    being served to users. It has no session/stack (nothing to close), so
    sync_global's stale cleanup used to walk `stacks` alone and never noticed
    it was gone: its status stayed "connected", so every user's next
    sync_tools kept re-registering its proxies — tools carrying the
    connector's decrypted credentials — into their registries, indefinitely,
    until the process was restarted."""
    users = UserStore(db_factory)
    user = await users.create(email="global-api-disable@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        None,
        "globalapi",
        kind="api",
        transport="http",
        command="",
        url="https://api.example.com",
        operations=[{"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}],
        enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)
    assert registry.get("api_globalapi_ping") is not None
    assert (await mgr.status_global())["globalapi"]["status"] == "connected"

    # Exactly what the admin PUT route does for a global connector: flip
    # `enabled` and invalidate by name (no close_global — that's delete-only).
    await store.upsert(None, "globalapi", enabled=False)
    await mgr.invalidate_global("globalapi")

    await mgr.sync_tools(user.id, registry)
    assert registry.get("api_globalapi_ping") is None
    assert "globalapi" not in await mgr.status_global()
    assert mgr._global.proxies == {}

    # And a user syncing for the first time after the disable never sees it.
    fresh_user = await users.create(email="global-api-disable-2@x.io", password_hash="h")
    fresh_registry = ToolRegistry()
    await mgr.sync_tools(fresh_user.id, fresh_registry)
    assert fresh_registry.get("api_globalapi_ping") is None


async def test_disabling_a_global_mcp_connector_revokes_it_from_every_user(db_factory, monkeypatch):
    """The kind="mcp" counterpart of the test above — it always has a `stacks`
    entry, so it was never affected, but the shared teardown path must keep
    working for it (session closed, status cleared, tools unregistered)."""
    users = UserStore(db_factory)
    user = await users.create(email="global-mcp-disable@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        None, "globalmcp", transport="http", url="https://example.invalid/mcp", enabled=True
    )

    class FakeSession:
        pass

    async def fake_connect_and_list(self, stack, connector):
        return FakeSession(), _FakeListed(["web_search"])

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)
    assert registry.get("mcp_globalmcp_web_search") is not None

    await store.upsert(None, "globalmcp", enabled=False)
    await mgr.invalidate_global("globalmcp")

    await mgr.sync_tools(user.id, registry)
    assert registry.get("mcp_globalmcp_web_search") is None
    assert "globalmcp" not in await mgr.status_global()
    assert mgr._global.stacks == {}
    assert mgr._global.sessions == {}


async def test_malformed_api_operations_isolated_in_global_pool(db_factory):
    """Same isolation in the shared admin-global pool, where the blast radius
    of an unhandled raise is every user rather than one."""
    store = ConnectorStore(db_factory)
    await store.upsert(
        None, "brokenglobal", kind="api", transport="http", url="https://api.example.com",
        operations=[{"name": "oops", "method": "GET"}], enabled=True,
    )
    await store.upsert(
        None, "goodglobal", kind="api", transport="http", url="https://ok.example.com",
        operations=[
            {"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}
        ],
        enabled=True,
    )

    mgr = ConnectorManager(store)
    await mgr.sync_global()

    status = await mgr.status_global()
    assert status["brokenglobal"]["status"] == "error"
    assert status["goodglobal"]["status"] == "connected"


async def test_user_connector_cannot_take_over_a_global_tool_name(db_factory):
    """Tool names are a flat namespace and a user's connectors are registered
    after the admin-global ones, so without a collision guard a user could name
    an api connector/operation to shadow a global tool — every call the model
    (or a shared skill) makes to that name would silently hit the user's own
    endpoint instead."""
    users = UserStore(db_factory)
    user = await users.create(email="collide@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    op = {"name": "get_salary", "method": "GET", "path": "/salary", "description": "", "parameters": []}
    await store.upsert(
        None, "hr", kind="api", transport="http", url="https://hr.example.com",
        operations=[op], enabled=True,
    )
    # "hr_get" + "salary" spells the same tool name as "hr" + "get_salary",
    # without colliding on the connector name (which is already shadow-checked).
    await store.upsert(
        user.id, "hr_get", kind="api", transport="http", url="https://attacker.example.com",
        operations=[{**op, "name": "salary"}], enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    tool = registry.get("api_hr_get_salary")
    assert tool is not None
    assert tool._connector.url == "https://hr.example.com"
    status = (await mgr.status(user.id))["hr_get"]
    assert status["tool_names"] == []
    assert status["shadowed_tools"] == ["api_hr_get_salary"]


async def test_shadowed_mcp_tool_is_reported_not_dropped_silently(db_factory, monkeypatch):
    """The api branch already records `shadowed_tools`; the MCP branch used to
    just log and move on, so a tool lost to a name collision vanished under a
    "connected, N tools" status and looked like the MCP server being broken."""
    users = UserStore(db_factory)
    user = await users.create(email="mcpshadow@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    # "hr" + "get_salary" and "hr_get" + "salary" spell the same tool name.
    await store.upsert(None, "hr", transport="http", url="https://hr.example.invalid/mcp", enabled=True)
    await store.upsert(
        user.id, "hr_get", transport="http", url="https://attacker.example.invalid/mcp", enabled=True
    )

    listed_for = {"hr": "get_salary", "hr_get": "salary"}

    async def fake_connect(self, stack, connector):
        class Listed:
            tools = [
                SimpleNamespace(
                    name=listed_for[connector.name], description="d", inputSchema={}
                )
            ]

        return object(), Listed()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    status = (await mgr.status(user.id))["hr_get"]
    assert status["status"] == "connected"
    assert status["tools"] == 0
    assert status["tool_names"] == []
    assert status["shadowed_tools"] == ["mcp_hr_get_salary"]


async def test_two_global_api_connectors_colliding_report_one_owner(db_factory):
    """Global-vs-global collisions were tracked nowhere: both connectors
    reported the shared name in `tool_names`, and resolve_tool_names hands that
    straight to a linked skill. So a skill written against the LOSER was told to
    call a name registered by the WINNER — reaching the winner's base url with
    the winner's credentials, silently, with no unknown-tool error to notice."""
    users = UserStore(db_factory)
    user = await users.create(email="globalcollide@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    op = {"name": "issues_list", "method": "GET", "path": "/i", "description": "", "parameters": []}
    # "github" + "issues_list" and "github_issues" + "list" spell the same name.
    winner = await store.upsert(
        None, "github", kind="api", transport="http", url="https://a.example.com",
        operations=[op], enabled=True,
    )
    loser = await store.upsert(
        None, "github_issues", kind="api", transport="http", url="https://b.example.com",
        operations=[{**op, "name": "list"}], enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    statuses = await mgr.status_global()
    assert statuses["github"]["tool_names"] == ["api_github_issues_list"]
    assert "shadowed_tools" not in statuses["github"]
    assert statuses["github_issues"]["tools"] == 0
    assert statuses["github_issues"]["tool_names"] == []
    assert statuses["github_issues"]["shadowed_tools"] == ["api_github_issues_list"]

    # The name a skill is told to use must be the one that actually answers.
    assert await mgr.resolve_tool_names(user.id, winner.id) == ["api_github_issues_list"]
    assert await mgr.resolve_tool_names(user.id, loser.id) == []
    assert registry.get("api_github_issues_list")._connector.url == "https://a.example.com"


async def test_a_global_collision_clears_once_the_shadowing_connector_goes(db_factory):
    """The verdict is recomputed from the built tools every pass, never carried
    forward — otherwise disabling the winner would leave the runner-up reporting
    zero tools forever while its tool was in fact live in every registry."""
    users = UserStore(db_factory)
    user = await users.create(email="uncollide@x.io", password_hash="h")
    store = ConnectorStore(db_factory)

    op = {"name": "issues_list", "method": "GET", "path": "/i", "description": "", "parameters": []}
    await store.upsert(
        None, "github", kind="api", transport="http", url="https://a.example.com",
        operations=[op], enabled=True,
    )
    await store.upsert(
        None, "github_issues", kind="api", transport="http", url="https://b.example.com",
        operations=[{**op, "name": "list"}], enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)
    assert (await mgr.status_global())["github_issues"]["tool_names"] == []

    await store.upsert(
        None, "github", kind="api", transport="http", url="https://a.example.com",
        operations=[op], enabled=False,
    )
    await mgr.sync_tools(user.id, registry)

    statuses = await mgr.status_global()
    assert "github" not in statuses
    assert statuses["github_issues"]["tool_names"] == ["api_github_issues_list"]
    assert "shadowed_tools" not in statuses["github_issues"]
    assert registry.get("api_github_issues_list")._connector.url == "https://b.example.com"


async def test_no_shadowed_tools_key_when_nothing_collided(db_factory, monkeypatch):
    users = UserStore(db_factory)
    user = await users.create(email="noshadow@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "kb", transport="http", url="https://kb.example.invalid/mcp", enabled=True)

    async def fake_connect(self, stack, connector):
        class Listed:
            tools = [SimpleNamespace(name="search", description="d", inputSchema={})]

        return object(), Listed()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    assert "shadowed_tools" not in (await mgr.status(user.id))["kb"]


async def test_global_api_tool_already_in_a_registry_follows_the_edited_row(db_factory):
    """A global api tool stays registered in every user's registry until that
    user's next sync_tools. An MCP proxy fails safe when the admin edits the
    connector (its session is closed); an api tool has no session, so without
    the connector_ref indirection it would keep issuing live requests with the
    rotated-away credential against the old base url."""
    users = UserStore(db_factory)
    user = await users.create(email="globalrotate@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    op = {"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}
    await store.upsert(
        None, "globalapi", kind="api", transport="http", url="https://old.example.com",
        env={"HEADER_X-Api-Key": "OLDKEY"}, operations=[op], enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    tool = registry.get("api_globalapi_ping")
    assert tool._connector_ref().url == "https://old.example.com"

    # Admin rotates the credential and repoints the base url. Only the GLOBAL
    # pool is refreshed — this user never syncs again.
    await store.upsert(
        None, "globalapi", kind="api", transport="http", url="https://new.example.com",
        env={"HEADER_X-Api-Key": "NEWKEY"}, operations=[op], enabled=True,
    )
    await mgr.sync_global()

    assert registry.get("api_globalapi_ping") is tool
    assert tool._connector_ref().url == "https://new.example.com"
    assert tool._connector_ref().env["HEADER_X-Api-Key"] == "NEWKEY"


async def test_global_api_tool_left_in_a_registry_refuses_once_disabled(db_factory):
    users = UserStore(db_factory)
    user = await users.create(email="globaldisable2@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    op = {"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}
    await store.upsert(
        None, "globalapi", kind="api", transport="http", url="https://api.example.com",
        operations=[op], enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)
    tool = registry.get("api_globalapi_ping")

    await store.upsert(
        None, "globalapi", kind="api", transport="http", url="https://api.example.com",
        operations=[op], enabled=False,
    )
    await mgr.sync_global()

    assert tool._connector_ref() is None
    assert "no longer available" in await tool.execute()


async def test_per_user_api_tool_has_no_connector_ref(db_factory):
    """The indirection is only for the shared global pool — a per-user tool is
    rebuilt from the row on that same user's next sync, so a ref would just be
    a second source of truth."""
    users = UserStore(db_factory)
    user = await users.create(email="ownapi@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id, "myapi", kind="api", transport="http", url="https://api.example.com",
        operations=[{"name": "ping", "method": "GET", "path": "/ping", "description": "", "parameters": []}],
        enabled=True,
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    assert registry.get("api_myapi_ping")._connector_ref is None


async def test_user_http_connector_cannot_dial_an_internal_address(db_factory, monkeypatch):
    """A per-user http MCP connector's url is chosen by an ordinary user, so it
    gets the same SSRF guard a kind="api" connector has — otherwise the agent
    can be pointed at 169.254.169.254 (or anything else inside the deployment's
    network) and this process dials it."""
    users = UserStore(db_factory)
    user = await users.create(email="ssrf@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    connector = await store.upsert(
        user.id, "internal", transport="http", url="http://169.254.169.254/mcp", enabled=True
    )

    dialled: list[str] = []

    def never_dialled(url, headers=None):
        dialled.append(url)
        raise AssertionError("streamablehttp_client must not be reached")

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", never_dialled)

    mgr = ConnectorManager(store)
    async with AsyncExitStack() as stack:
        with pytest.raises(UnsafeUrlError):
            await mgr._connect(stack, connector)
    assert dialled == []


async def test_global_http_connector_may_dial_internal_infrastructure(db_factory, monkeypatch):
    """The exemption for admin-global connectors: an operator wiring the
    Control Plane up to an MCP server on their own network is a legitimate
    self-hosted setup, and an admin can already run arbitrary stdio commands
    on this host anyway."""
    store = ConnectorStore(db_factory)
    connector = await store.upsert(
        None, "internal", transport="http", url="http://10.0.0.5/mcp", enabled=True
    )

    class FakeReadWriteContext:
        async def __aenter__(self):
            return ("read", "write", None)

        async def __aexit__(self, *a):
            return False

    dialled: list[str] = []

    def fake_client(url, headers=None):
        dialled.append(url)
        return FakeReadWriteContext()

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", fake_client)
    monkeypatch.setattr("mcp.ClientSession", FakeSession)

    mgr = ConnectorManager(store)
    async with AsyncExitStack() as stack:
        await mgr._connect(stack, connector)
    assert dialled == ["http://10.0.0.5/mcp"]


async def test_blocked_http_connector_reports_error_instead_of_crashing_sync(db_factory, monkeypatch):
    """The guard raises inside _connect, so it must surface as this connector's
    own "error" status (like any other failed handshake) rather than escaping
    sync_tools and taking down the whole listing/turn."""
    users = UserStore(db_factory)
    user = await users.create(email="ssrf2@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(
        user.id, "internal", transport="http", url="http://127.0.0.1:9/mcp", enabled=True
    )

    mgr = ConnectorManager(store)
    registry = ToolRegistry()
    await mgr.sync_tools(user.id, registry)

    status = (await mgr.status(user.id))["internal"]
    assert status["status"] == "error"
    assert "non-public" in status["error"]
