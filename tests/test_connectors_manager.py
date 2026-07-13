"""ConnectorManager.sync_tools: a single connector that hangs mid-handshake
must not block the per-user lock forever — every subsequent /connectors
listing (or chat turn) for that user would otherwise hang indefinitely too,
until the process is restarted (the actual incident this guards against)."""

import asyncio

import claw.core.connectors as connectors_module
from claw.core.connectors import ConnectorManager, McpToolProxy
from claw.db.stores import ConnectorStore, UserStore
from claw.tools.registry import ToolRegistry


async def test_hanging_connector_times_out_instead_of_blocking_forever(db_factory, monkeypatch):
    monkeypatch.setattr(connectors_module, "_CONNECT_TIMEOUT_SECONDS", 0.05)

    users = UserStore(db_factory)
    user = await users.create(email="hang@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    await store.upsert(user.id, "stuck", transport="http", url="https://example.invalid/mcp", enabled=True)

    async def hangs_forever(self, stack, connector):
        await asyncio.sleep(10)  # far longer than the patched timeout
        raise AssertionError("should have been cancelled by the timeout")

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", hangs_forever)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()

    await asyncio.wait_for(mgr.sync_tools(user.id, registry), timeout=2)

    status = await mgr.status(user.id)
    assert status["stuck"]["status"] == "error"
    assert "timed out after 0.05s" in status["stuck"]["error"]


async def test_second_connector_still_syncs_after_first_one_times_out(db_factory, monkeypatch):
    monkeypatch.setattr(connectors_module, "_CONNECT_TIMEOUT_SECONDS", 0.05)

    users = UserStore(db_factory)
    user = await users.create(email="partial@x.io", password_hash="h")
    store = ConnectorStore(db_factory)
    # Alphabetically first, so it's attempted (and times out) before "working".
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
        return FakeSession(), await FakeSession().list_tools()

    monkeypatch.setattr(ConnectorManager, "_connect_and_list", fake_connect_and_list)

    mgr = ConnectorManager(store)
    registry = ToolRegistry()

    await asyncio.wait_for(mgr.sync_tools(user.id, registry), timeout=2)

    status = await mgr.status(user.id)
    assert status["broken"]["status"] == "error"
    assert status["working"]["status"] == "connected"


async def test_tool_call_times_out_instead_of_hanging_the_turn_forever(monkeypatch):
    """A connected session can still hang on an individual call_tool (e.g. the
    remote server sends a malformed response the client never returns from) —
    this must surface as a normal tool error, not spin the chat turn forever."""
    monkeypatch.setattr(connectors_module, "_TOOL_CALL_TIMEOUT_SECONDS", 0.05)

    class HangingSession:
        async def call_tool(self, name, kwargs):
            await asyncio.sleep(10)

    proxy = McpToolProxy(HangingSession(), "softnixkb", "search_knowledge", "desc", {})
    result = await asyncio.wait_for(proxy.execute(query="x"), timeout=2)

    assert result.startswith("Error:")
    assert "timed out after 0.05s" in result


async def test_tool_call_returns_normally_when_within_the_timeout():
    class FakeContentItem:
        text = "the answer"

    class FakeResult:
        content = [FakeContentItem()]
        isError = False

    class FastSession:
        async def call_tool(self, name, kwargs):
            return FakeResult()

    proxy = McpToolProxy(FastSession(), "softnixkb", "search_knowledge", "desc", {})
    result = await proxy.execute(query="x")

    assert result == "the answer"
