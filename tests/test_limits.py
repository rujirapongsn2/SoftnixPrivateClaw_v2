from claw.core.limits import RateLimiter


def test_rate_limiter_allows_up_to_limit_then_blocks():
    rl = RateLimiter(per_minute=3)
    assert [rl.allow("u1") for _ in range(3)] == [True, True, True]
    assert rl.allow("u1") is False  # 4th in the same minute


def test_rate_limiter_is_per_user():
    rl = RateLimiter(per_minute=1)
    assert rl.allow("a") is True
    assert rl.allow("b") is True  # separate bucket
    assert rl.allow("a") is False


def test_rate_limiter_unlimited_when_zero():
    rl = RateLimiter(per_minute=0)
    assert all(rl.allow("u") for _ in range(100))


async def test_agent_eviction_bounds_memory(tmp_path, stores):
    from claw.config import SandboxSettings, Settings
    from claw.core.bus import EventBus
    from claw.core.memory import MemoryService
    from claw.core.runtime import AgentRuntime
    from tests.conftest import FakeProvider

    settings = Settings(
        _env_file=None,
        workspaces_root=tmp_path / "ws",
        sandbox=SandboxSettings(enabled=False),
        max_resident_agents=2,
    )
    provider = FakeProvider([])
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    rt = AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(),
        users=stores["users"], sessions=stores["sessions"], messages=stores["messages"],
        memory=memory, audit=stores["audit"],
    )
    a = rt.get_agent("u1")
    rt.get_agent("u2")
    rt.get_agent("u3")  # evicts u1 (least recently used)
    assert set(rt._agents.keys()) == {"u2", "u3"}
    # Re-requesting u1 builds a fresh instance.
    assert rt.get_agent("u1") is not a


async def test_rate_limited_turn_skips_model(tmp_path, stores):
    from claw.config import SandboxSettings, Settings
    from claw.core.bus import EventBus
    from claw.core.memory import MemoryService
    from claw.core.runtime import AgentRuntime
    from tests.conftest import FakeProvider, text_turn

    settings = Settings(
        _env_file=None, workspaces_root=tmp_path / "ws",
        sandbox=SandboxSettings(enabled=False), turns_per_minute=1,
    )
    provider = FakeProvider([text_turn("hi"), text_turn("second")])
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    rt = AgentRuntime(
        settings=settings, provider=provider, bus=EventBus(),
        users=stores["users"], sessions=stores["sessions"], messages=stores["messages"],
        memory=memory, audit=stores["audit"],
    )
    user = await stores["users"].get_or_create_by_email("u@x.y")
    s = await stores["sessions"].create(user.id)

    first = await rt.handle_message(user.id, s.id, "one")
    assert first == "hi"
    second = await rt.handle_message(user.id, s.id, "two")
    assert "too fast" in second.lower()
    # The second turn never reached the model.
    assert len(provider.calls) == 1
