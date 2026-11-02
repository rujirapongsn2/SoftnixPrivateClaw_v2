"""Agent Channel: an external agent chats as the key's owner, limited to the
tools of the connectors the owner picked (claw/channels/agent.py)."""

import asyncio
import json
import importlib.util
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import claw.channels.agent as agent_module
from claw.channels.agent import SAFE_TOOLS, AgentBusy, AgentChannelService
from claw.core.bus import EventBus
from claw.core.events import TurnError
from claw.core.loop import AgentLoop
from claw.core.runtime import TurnFailed
from claw.db.models import Base, McpConnector
from claw.db.stores import AgentChannelStore, AuditStore, ConnectorStore
from claw.providers.base import ChatResult, ToolCall
from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry
from tests.conftest import text_turn
from tests.conftest_app import build_api_app, client


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from claw.api import agent_channel as api_module

    api_module._failed_auth._windows.clear()
    api_module._per_client._windows.clear()


# ---------------------------------------------------------------- loop limit

class _Probe(Tool):
    description = "probe"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, ran: list[str]):
        self.name = name
        self._ran = ran

    async def execute(self, **_: Any) -> str:
        self._ran.append(self.name)
        return "ran"


class _RecordingProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.offered: list[list[str]] = []

    async def stream_chat(self, messages, tools=None, **_):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        for event in self.turns.pop(0):
            yield event

    def count_tokens(self, messages, model=None):
        return 1


async def test_allowed_tools_hides_and_refuses_everything_else():
    ran: list[str] = []
    registry = ToolRegistry()
    for name in ("mcp_crm_search", "exec", "write_file"):
        registry.register(_Probe(name, ran))
    calls = [ToolCall(id="1", name="exec", arguments={}), ToolCall(id="2", name="mcp_crm_search", arguments={})]
    provider = _RecordingProvider([[ChatResult(content="", tool_calls=calls)], text_turn("done")])

    outcome = await AgentLoop(provider, registry).run_turn(
        "t1", [{"role": "user", "content": "hi"}], lambda _e: None,
        allowed_tools=frozenset({"mcp_crm_search"}),
    )

    assert provider.offered[0] == ["mcp_crm_search"]
    assert ran == ["mcp_crm_search"]
    refused = next(m for m in outcome.new_messages if m.get("tool_call_id") == "1")
    assert "not available" in refused["content"]


async def test_without_a_limit_every_tool_is_offered():
    registry = ToolRegistry()
    for name in ("mcp_crm_search", "exec"):
        registry.register(_Probe(name, []))
    provider = _RecordingProvider([text_turn("hi")])
    await AgentLoop(provider, registry).run_turn("t1", [{"role": "user", "content": "hi"}], lambda _e: None)
    assert provider.offered[0] == ["exec", "mcp_crm_search"]


# ---------------------------------------------------------------- service

class _FakeManager:
    def __init__(self, names_by_id):
        self.names_by_id = names_by_id

    async def resolve_tool_names(self, user_id, connector_id, *, owned=None, global_connectors=None):
        return self.names_by_id.get(connector_id)


class _FakeRuntime:
    def __init__(self, reply="ok", delay=0.0, fail=None, turn_error=None):
        self.reply, self.delay, self.fail, self.turn_error = reply, delay, fail, turn_error
        self.calls: list[dict] = []
        self.connectors = _FakeManager({"c1": ["mcp_crm_search", "mcp_crm_update"]})
        self.bus = EventBus()

    async def warm_connectors(self, user_id):
        return None

    async def handle_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        if self.turn_error:
            self.bus.publish(kwargs["session_id"], TurnError(turn_id="t", message=self.turn_error))
            return self.turn_error
        return self.reply


async def _service(db_factory, stores, runtime, connector_ids=("c1",)):
    store = AgentChannelStore(db_factory, "test-secret")
    owner = await stores["users"].get_or_create_by_email("owner@x.y")
    service = AgentChannelService(
        runtime, store, stores["sessions"], stores["users"], ConnectorStore(db_factory), AuditStore(db_factory, is_postgres=False)
    )
    async with db_factory() as db:
        for cid in connector_ids:
            if await db.get(McpConnector, cid) is None:
                db.add(McpConnector(id=cid, owner_id=owner.id, name="crm", transport="http", url="https://x", env={}, enabled=True))
        await db.commit()
    row, key = await store.create_client(owner.id, "Grok", list(connector_ids), None)
    return service, store, owner, row, key


async def test_a_deleted_connector_grants_nothing(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    await service.allowed_tools(row)
    async with db_factory() as db:
        await db.delete(await db.get(McpConnector, "c1"))
        await db.commit()
    assert await service.allowed_tools(row) == SAFE_TOOLS


async def test_a_message_runs_as_the_owner_with_only_its_connector_tools(db_factory, stores):
    runtime = _FakeRuntime(reply="42 open deals")
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)

    turn = await service.send(row, "thread-1", "how many deals?")

    assert (turn.status, turn.reply) == ("completed", "42 open deals")
    call = runtime.calls[0]
    assert call["user_id"] == owner.id and call["channel"] == "agent"
    assert call["allowed_tools"] == SAFE_TOOLS | {"mcp_crm_search", "mcp_crm_update"}
    session = await stores["sessions"].get(call["session_id"])
    assert session.user_id == owner.id and session.channel == "agent"


async def test_a_conversation_keeps_its_session_and_another_gets_its_own(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    await service.send(row, "thread-1", "a")
    await service.send(row, "thread-1", "b")
    await service.send(row, "thread-2", "c")
    sessions = [c["session_id"] for c in runtime.calls]
    assert sessions[0] == sessions[1] != sessions[2]


async def test_a_deleted_chat_gets_a_new_session(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    await service.send(row, "t", "a")
    await stores["sessions"].delete(runtime.calls[0]["session_id"])
    await service.send(row, "t", "b")
    assert runtime.calls[1]["session_id"] != runtime.calls[0]["session_id"]


async def test_one_message_at_a_time_per_client_and_a_retry_gets_the_same_turn(db_factory, stores, monkeypatch):
    monkeypatch.setattr(agent_module, "WAIT_SECONDS", 0.05)
    runtime = _FakeRuntime(delay=0.3)
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)

    first = await service.send(row, "t", "slow", idempotency_key="k1")
    assert first.status == "working"
    again = await service.send(row, "t", "slow", idempotency_key="k1")
    assert again.id == first.id
    try:
        await service.send(row, "t", "other")
        raise AssertionError("expected AgentBusy")
    except AgentBusy:
        pass

    await asyncio.sleep(0.4)
    done = await store.get_turn(row.id, first.id)
    assert (done.status, done.reply) == ("completed", "ok")
    assert len(runtime.calls) == 1
    assert (await service.send(row, "t", "slow", idempotency_key="k1")).id == first.id
    assert len(runtime.calls) == 1


async def test_a_failed_turn_reports_the_reason(db_factory, stores):
    runtime = _FakeRuntime(fail=TurnFailed("Daily message limit reached."))
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    turn = await service.send(row, "t", "x")
    assert (turn.status, turn.reply) == ("failed", "Daily message limit reached.")
    assert row.id not in service._busy


async def test_a_model_failure_is_reported_as_failed_not_completed(db_factory, stores):
    runtime = _FakeRuntime(turn_error="The AI model could not be reached.")
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    turn = await service.send(row, "t", "x")
    assert (turn.status, turn.reply) == ("failed", "The AI model could not be reached.")


async def test_no_connectors_means_only_the_safe_built_ins(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime, connector_ids=())
    await service.send(row, "t", "x")
    assert runtime.calls[0]["allowed_tools"] == SAFE_TOOLS
    assert "exec" not in SAFE_TOOLS


async def test_only_turns_older_than_the_stale_limit_are_failed(db_factory, stores):
    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime())
    old, _ = await store.start_turn(row.id, "t", "old")
    fresh, _ = await store.start_turn(row.id, "t", "fresh")
    async with db_factory() as db:
        from claw.db.models import AgentTurn
        (await db.get(AgentTurn, old.id)).created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()
    await store.fail_stale_turns(datetime.now(timezone.utc) - agent_module.TURN_STALE_AFTER, row.id)
    assert (await store.get_turn(row.id, old.id)).status == "failed"
    assert (await store.get_turn(row.id, fresh.id)).status == "working"


async def test_a_turn_running_in_another_process_makes_the_key_busy(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    await store.start_turn(row.id, "t", None)  # recorded by another process, still working
    try:
        await service.send(row, "t", "x")
        raise AssertionError("expected AgentBusy")
    except AgentBusy:
        pass
    assert runtime.calls == []


async def test_new_conversations_are_capped_per_key(db_factory, stores, monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_CONVERSATIONS", 2)
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    await service.send(row, "a", "x")
    await service.send(row, "b", "x")
    try:
        await service.send(row, "c", "x")
        raise AssertionError("expected ConversationLimit")
    except agent_module.ConversationLimit:
        pass
    assert (await service.send(row, "a", "again")).status == "completed"
    assert row.id not in service._busy


# ---------------------------------------------------------------- keys

async def test_only_a_keyed_hash_is_stored_and_bad_keys_fail(db_factory, stores):
    service, store, owner, row, key = await _service(db_factory, stores, _FakeRuntime())
    assert key not in (row.key_hash, row.key_prefix) and key.startswith(f"pcak_{row.key_prefix}_")
    assert (await store.authenticate(key)).id == row.id
    assert await store.authenticate(key + "x") is None
    assert await store.authenticate("pcak_nope_nope") is None
    assert await AgentChannelStore(db_factory, "other-secret").authenticate(key) is None
    await store.revoke(row.id)
    assert await store.authenticate(key) is None


async def test_an_expired_key_fails(db_factory, stores):
    store = AgentChannelStore(db_factory, "s")
    owner = await stores["users"].get_or_create_by_email("e@x.y")
    _, key = await store.create_client(owner.id, "old", [], datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await store.authenticate(key) is None


# ---------------------------------------------------------------- HTTP

def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _register(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


async def test_http_flow_switch_keys_and_messages(db_factory):
    app = build_api_app(db_factory)
    state = app.state.claw
    runtime = _FakeRuntime(reply="pong")
    state.agent_channel = AgentChannelService(
        runtime, state.agent_channel_store, state.sessions, state.users, state.connectors, state.audit
    )
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")

        assert (await c.post("/api/agent-clients", json={"name": "Grok"}, headers=_bearer(user))).status_code == 403
        assert (await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(user))).status_code == 403
        assert (await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))).status_code == 200
        assert (await c.get("/api/features", headers=_bearer(user))).json()["agent_channel"] is True

        bad = await c.post("/api/agent-clients", json={"name": "Grok", "connector_ids": ["not-mine"]}, headers=_bearer(user))
        assert bad.status_code == 422
        created = (await c.post("/api/agent-clients", json={"name": "Grok"}, headers=_bearer(user))).json()
        key = created["key"]
        listed = (await c.get("/api/agent-clients", headers=_bearer(user))).json()
        assert key not in str(listed) and listed["clients"][0]["status"] == "active"

        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"})).status_code == 401
        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"}, headers=_bearer(user))).status_code == 401
        r = await c.post("/api/agent/v1/messages", json={"message": "ping", "conversation_id": "g-1"}, headers=_bearer(key))
        assert r.status_code == 200 and r.json()["reply"] == "pong" and r.json()["status"] == "completed"
        polled = await c.get(f"/api/agent/v1/turns/{r.json()['turn_id']}", headers=_bearer(key))
        assert polled.json()["reply"] == "pong"
        assert (await c.post("/api/agent/v1/messages", json={"message": "x", "conversation_id": "bad id!"},
                             headers=_bearer(key))).status_code == 422

        admin_view = (await c.get("/api/admin/agent-channel", headers=_bearer(admin))).json()
        assert admin_view["clients"][0]["owner_email"] == "user@x.io"

        await c.put("/api/admin/agent-channel", json={"enabled": False}, headers=_bearer(admin))
        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"}, headers=_bearer(key))).status_code == 403
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))

        cid = created["client"]["id"]
        assert (await c.delete(f"/api/agent-clients/{cid}", headers=_bearer(admin))).status_code == 404
        assert (await c.delete(f"/api/agent-clients/{cid}", headers=_bearer(user))).status_code == 200
        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"}, headers=_bearer(key))).status_code == 401


async def test_connectors_offered_are_the_users_own_and_global_ones(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        me = (await c.get("/api/me", headers=_bearer(user))).json()
        other = (await c.get("/api/me", headers=_bearer(admin))).json()
        async with db_factory() as db:
            for cid, owner in (("own", me["id"]), ("glob", None), ("theirs", other["id"])):
                db.add(McpConnector(id=cid, owner_id=owner, name=cid, transport="http", url="https://x", env={}, enabled=True))
            await db.commit()
        listed = (await c.get("/api/agent-clients", headers=_bearer(user))).json()["connectors"]
        assert {x["id"] for x in listed} == {"own", "glob"}
        ok = await c.post("/api/agent-clients", json={"name": "G", "connector_ids": ["own", "glob"]}, headers=_bearer(user))
        assert ok.status_code == 200
        theirs = await c.post("/api/agent-clients", json={"name": "G", "connector_ids": ["theirs"]}, headers=_bearer(user))
        assert theirs.status_code == 422


# ---------------------------------------------------------------- migration

def test_migration_creates_and_drops_the_tables(tmp_path):
    spec = importlib.util.spec_from_file_location("m", "migrations/versions/f1c4a8e2b7d3_agent_channel.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path}/m.db")
    legacy = sa.MetaData()
    sa.Table("users", legacy, sa.Column("id", sa.String(32), primary_key=True))
    legacy.create_all(engine)

    def run(step):
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    run(migration.upgrade)
    tables = set(sa.inspect(engine).get_table_names())
    assert {"agent_clients", "agent_conversations", "agent_turns"} <= tables
    for table in ("agent_clients", "agent_conversations", "agent_turns"):
        model_cols = {c.name for c in Base.metadata.tables[table].columns}
        assert model_cols == {c["name"] for c in sa.inspect(engine).get_columns(table)}, table
    run(migration.downgrade)
    assert not {"agent_clients", "agent_conversations", "agent_turns"} & set(sa.inspect(engine).get_table_names())


async def test_the_real_runtime_offers_and_runs_only_allowed_tools(stores, tmp_path):
    from tests.test_runtime import make_runtime

    calls = [ToolCall(id="1", name="exec", arguments={"command": "id"})]
    provider = _RecordingProvider([[ChatResult(content="", tool_calls=calls)], text_turn("done")])
    runtime = make_runtime(stores, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("agent-owner@x.y")
    session = await stores["sessions"].create(user.id, channel="agent")
    assert "exec" in runtime.get_agent(user.id).tools.tool_names

    reply = await runtime.handle_message(
        user_id=user.id, session_id=session.id, content="run id", channel="agent",
        allowed_tools=frozenset({"update_plan"}),
    )

    assert reply == "done"
    assert all(set(offered) <= {"update_plan"} for offered in provider.offered)
    history = json.dumps(await stores["messages"].recent(session.id), ensure_ascii=False)
    assert "not available to this caller" in history


# ---------------------------------------------------------------- review fixes

async def test_two_messages_arriving_together_run_one_at_a_time(db_factory, stores, monkeypatch):
    monkeypatch.setattr(agent_module, "WAIT_SECONDS", 0.05)
    runtime = _FakeRuntime(delay=0.2)
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    service._pruned_at = datetime.now(timezone.utc)  # both requests reach the busy check together
    results = await asyncio.gather(
        service.send(row, "t", "a"), service.send(row, "t", "b"), return_exceptions=True
    )
    assert sum(isinstance(r, AgentBusy) for r in results) == 1
    await asyncio.sleep(0.3)
    assert len(runtime.calls) == 1


async def test_a_conflicting_insert_never_returns_a_missing_turn(db_factory, stores, monkeypatch):
    store = AgentChannelStore(db_factory, "s")
    owner = await stores["users"].get_or_create_by_email("i@x.y")
    row, _ = await store.create_client(owner.id, "g", [], None)
    first, created = await store.start_turn(row.id, "t", "k")
    again, created_again = await store.start_turn(row.id, "t", "k")
    assert created and not created_again and again.id == first.id

    async def gone(*_):
        return None

    monkeypatch.setattr(store, "find_turn", gone)  # e.g. pruned between the insert and the lookup
    try:
        await store.start_turn(row.id, "t", "k")
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


async def test_tool_names_are_cached_briefly_per_client(db_factory, stores):
    runtime = _FakeRuntime()
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    first = await service.allowed_tools(row)
    runtime.connectors.names_by_id["c1"] = ["mcp_crm_other"]
    assert await service.allowed_tools(row) == first
    # A rename (or delete and re-create under the same name) re-resolves at once.
    async with db_factory() as db:
        (await db.get(McpConnector, "c1")).name = "crm2"
        await db.commit()
    assert "mcp_crm_other" in await service.allowed_tools(row)


async def test_a_long_streamed_turn_does_not_fill_the_event_queue(db_factory, stores):
    from claw.core.events import TextDeltaEvent

    class _Chatty(_FakeRuntime):
        async def handle_message(self, **kwargs):
            for i in range(3000):
                self.bus.publish(kwargs["session_id"], TextDeltaEvent(turn_id="t", text="x"))
                if i % 100 == 0:
                    await asyncio.sleep(0)
            return "long answer"

    service, store, owner, row, _ = await _service(db_factory, stores, _Chatty())
    turn = await service.send(row, "t", "x")
    assert (turn.status, turn.reply) == ("completed", "long answer")


async def test_agent_chats_never_reach_long_term_memory(stores, tmp_path):
    from tests.test_runtime import make_runtime

    runtime = make_runtime(stores, _RecordingProvider([text_turn("ok")]), tmp_path)
    passes: list[str] = []

    async def consolidate(user_id, session_id):
        passes.append(session_id)
        return False

    runtime.memory._consolidate = consolidate
    user = await stores["users"].get_or_create_by_email("mem@x.y")
    agent_session = await stores["sessions"].create(user.id, channel="agent")
    web_session = await stores["sessions"].create(user.id)
    await runtime.memory.maybe_consolidate(user.id, agent_session.id)
    await runtime.memory.maybe_consolidate(user.id, web_session.id)
    assert passes == [web_session.id]


async def test_a_tool_limited_turn_gets_no_memory_in_its_prompt(stores, tmp_path):
    from tests.test_runtime import make_runtime

    provider = _RecordingProvider([text_turn("ok")])
    seen: list[str] = []
    original = provider.stream_chat

    async def spy(messages, tools=None, **kwargs):
        seen.append(json.dumps(messages, ensure_ascii=False))
        async for event in original(messages, tools=tools, **kwargs):
            yield event

    provider.stream_chat = spy
    runtime = make_runtime(stores, provider, tmp_path)

    async def secret_context(user_id):
        return "PRIVATE-MEMORY-MARKER"

    runtime.memory.build_context = secret_context
    user = await stores["users"].get_or_create_by_email("mem2@x.y")
    session = await stores["sessions"].create(user.id, channel="agent")
    await runtime.handle_message(
        user_id=user.id, session_id=session.id, content="what do you remember?", channel="agent",
        allowed_tools=frozenset({"update_plan"}),
    )
    assert seen and "PRIVATE-MEMORY-MARKER" not in seen[0]


async def test_menu_acl_blocks_the_feature_not_only_the_menu(db_factory):
    app = build_api_app(db_factory)
    state = app.state.claw
    state.agent_channel = AgentChannelService(
        _FakeRuntime(), state.agent_channel_store, state.sessions, state.users, state.connectors, state.audit
    )
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        key = (await c.post("/api/agent-clients", json={"name": "G"}, headers=_bearer(user))).json()["key"]
        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"}, headers=_bearer(key))).status_code == 200

        await c.put("/api/admin/settings-acl", json={"scope": "global", "rules": {"agents": False}}, headers=_bearer(admin))
        assert (await c.post("/api/agent-clients", json={"name": "G2"}, headers=_bearer(user))).status_code == 403
        cid = (await c.get("/api/agent-clients", headers=_bearer(user))).json()["clients"][0]["id"]
        assert (await c.put(f"/api/agent-clients/{cid}", json={"connector_ids": []}, headers=_bearer(user))).status_code == 403
        assert (await c.post("/api/agent/v1/messages", json={"message": "hi"}, headers=_bearer(key))).status_code == 403


async def test_active_keys_are_capped_per_user(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        ids = []
        for i in range(AgentChannelStore.MAX_ACTIVE_PER_OWNER):
            r = await c.post("/api/agent-clients", json={"name": f"g{i}"}, headers=_bearer(user))
            ids.append(r.json()["client"]["id"])
        assert (await c.post("/api/agent-clients", json={"name": "one more"}, headers=_bearer(user))).status_code == 409
        await c.delete(f"/api/agent-clients/{ids[0]}", headers=_bearer(user))
        assert (await c.post("/api/agent-clients", json={"name": "one more"}, headers=_bearer(user))).status_code == 200
        listed = (await c.get("/api/agent-clients", headers=_bearer(user))).json()["clients"]
        assert listed[-1]["status"] == "revoked"
        view = (await c.get("/api/admin/agent-channel", headers=_bearer(admin))).json()
        assert view["total"] == AgentChannelStore.MAX_ACTIVE_PER_OWNER + 1


def test_an_agent_chat_is_read_only_from_the_web(db_factory):
    import asyncio as _asyncio

    from starlette.testclient import TestClient

    app = build_api_app(db_factory)
    state = app.state.claw

    class _WebRuntime:
        calls: list = []

        def pending_confirmations(self, session_id):
            return []

        async def active_artifact_events(self, user_id, session_id):
            return []

        def active_sessions(self):
            return set()

        async def handle_message(self, **kwargs):
            self.calls.append(kwargs)
            # Answer on the socket, so a turn that should not have run fails the test instead of hanging it.
            state.bus.publish(kwargs["session_id"], TurnError(turn_id="t", message="ran", code="ran"))
            return "ok"

    state.runtime = _WebRuntime()
    state.bus = EventBus()
    with TestClient(app) as tc:
        token = tc.post("/api/auth/register", json={"email": "web@x.io", "password": "password123"}).json()["access_token"]
        me = tc.get("/api/me", headers=_bearer(token)).json()
        agent_chat = _asyncio.run(state.sessions.create(me["id"], title="Grok · t", channel="agent")).id
        with tc.websocket_connect(f"/ws/chat/{agent_chat}?token={token}") as ws:
            ws.receive_json()  # session snapshot
            ws.send_json({"content": "run rm -rf"})
            reply = ws.receive_json()
        assert reply["type"] == "turn_error" and reply["code"] == "agent_chat_read_only"
        assert state.runtime.calls == []


async def test_changing_a_keys_connectors_is_audited(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        cid = (await c.post("/api/agent-clients", json={"name": "G"}, headers=_bearer(user))).json()["client"]["id"]
        await c.put(f"/api/agent-clients/{cid}", json={"connector_ids": []}, headers=_bearer(user))
    kinds = [row["kind"] for row in await app.state.claw.audit.list()]
    assert "agent_channel.key_connectors_changed" in kinds


async def test_failed_keys_are_braked_before_touching_the_database(db_factory, monkeypatch):
    from claw.api import agent_channel as api_module

    monkeypatch.setattr(api_module, "_failed_auth", api_module.RateLimiter(3))
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        codes = [
            (await c.post("/api/agent/v1/messages", json={"message": "x"}, headers=_bearer("pcak_a_b"))).status_code
            for _ in range(5)
        ]
    assert codes == [401, 401, 401, 429, 429]


async def test_the_switch_is_cached_and_refreshed_when_set(db_factory):
    store = AgentChannelStore(db_factory, "s")
    other_process = AgentChannelStore(db_factory, "s")
    assert await store.enabled() is False
    await other_process.set_enabled(True)
    assert await store.enabled() is False  # still the cached value, within the TTL
    assert await other_process.enabled() is True
    await store.set_enabled(False)
    assert await store.enabled() is False


async def test_active_keys_are_counted_in_sql_past_any_window(db_factory, stores):
    store = AgentChannelStore(db_factory, "s")
    owner = await stores["users"].get_or_create_by_email("cap@x.y")
    await store.create_client(owner.id, "live", [], None)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    for i in range(105):
        await store.create_client(owner.id, f"old{i}", [], past)
    assert await store.count_active(owner.id) == 1
