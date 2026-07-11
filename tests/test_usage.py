from claw.config import SandboxSettings, Settings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime
from claw.db.stores import UsageStore
from tests.conftest import FakeProvider, text_turn
from tests.conftest_app import build_api_app, client


async def test_usage_store_records_and_aggregates(db_factory, stores):
    usage = UsageStore(db_factory, is_postgres=False)
    await usage.record("u1", "s1", "gpt", {"prompt_tokens": 100, "completion_tokens": 20})
    await usage.record("u1", "s1", "gpt", {"prompt_tokens": 50, "completion_tokens": 10})
    await usage.record("u2", "s2", "gpt", {"prompt_tokens": 5, "completion_tokens": 1})

    u1 = await usage.totals_for_user("u1")
    assert u1 == {"prompt_tokens": 150, "completion_tokens": 30, "turns": 2}
    total = await usage.totals()
    assert total["prompt_tokens"] == 155 and total["turns"] == 3


async def test_zero_usage_not_recorded(db_factory):
    usage = UsageStore(db_factory, is_postgres=False)
    await usage.record("u1", "s1", "gpt", {"prompt_tokens": 0, "completion_tokens": 0})
    assert (await usage.totals())["turns"] == 0


async def test_runtime_records_usage(tmp_path, stores, db_factory):
    usage = UsageStore(db_factory, is_postgres=False)
    provider = FakeProvider([text_turn("hi")])  # text_turn reports 10 prompt / 5 completion
    settings = Settings(_env_file=None, workspaces_root=tmp_path / "ws", sandbox=SandboxSettings(enabled=False))
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    rt = AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(),
        users=stores["users"], sessions=stores["sessions"], messages=stores["messages"],
        memory=memory, audit=stores["audit"], usage=usage,
    )
    user = await stores["users"].get_or_create_by_email("u@x.y")
    s = await stores["sessions"].create(user.id)
    await rt.handle_message(user.id, s.id, "hello")

    # Background usage task runs after the turn; drain it.
    import asyncio

    for _ in range(20):
        totals = await usage.totals_for_user(user.id)
        if totals["turns"] > 0:
            break
        await asyncio.sleep(0.02)
    assert totals == {"prompt_tokens": 10, "completion_tokens": 5, "turns": 1}


async def test_usage_endpoint_and_admin_stats(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        r = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        token = r.json()["access_token"]
        uid = r.json()["user"]["id"]
        await app.state.claw.usage.record(uid, None, "gpt", {"prompt_tokens": 7, "completion_tokens": 3})

        me_usage = await c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
        assert me_usage.json() == {"prompt_tokens": 7, "completion_tokens": 3, "turns": 1}

        stats = await c.get("/api/admin/stats", headers={"Authorization": f"Bearer {token}"})
        assert stats.json()["prompt_tokens"] == 7 and stats.json()["completion_tokens"] == 3
