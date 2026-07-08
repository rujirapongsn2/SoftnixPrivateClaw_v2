"""recall_memory: search the append-only consolidated history (past-conversation
summaries) that isn't otherwise injected into context.
"""

import pytest

from claw.core.memory import MemoryService
from claw.db.stores import MemoryStore
from claw.tools.memory import RecallMemoryTool


@pytest.mark.asyncio
async def test_search_history_substring_sqlite(db_factory):
    store = MemoryStore(db_factory)
    await store.append_history("u1", "[2026-07-01] Decided to use Postgres for the report pipeline.")
    await store.append_history("u1", "[2026-07-02] Discussed the mobile layout and dark mode.")
    await store.append_history("u2", "[2026-07-02] Other user's private note about billing.")

    # SQLite path (tests) → substring match, scoped to the user.
    hits = await store.search_history("u1", "report", is_postgres=False)
    assert len(hits) == 1 and "report pipeline" in hits[0]
    # Never leaks another user's history.
    assert await store.search_history("u1", "billing", is_postgres=False) == []
    # Empty query is a no-op, not a full dump.
    assert await store.search_history("u1", "  ", is_postgres=False) == []


@pytest.mark.asyncio
async def test_recall_tool_returns_matches(db_factory):
    store = MemoryStore(db_factory)
    await store.append_history("u1", "[2026-07-01] Agreed the API key rotates every 90 days.")
    memory = MemoryService(store, None, None, None, is_postgres=False)
    tool = RecallMemoryTool(memory, "u1")

    # SQLite path is substring match (fuzzy pg_trgm runs only on Postgres), so
    # query with a literal substring of the stored entry.
    out = await tool.execute(query="API key")
    assert "90 days" in out

    miss = await tool.execute(query="something never discussed")
    assert "No past-conversation records" in miss

    assert (await tool.execute(query="")).startswith("Error")
