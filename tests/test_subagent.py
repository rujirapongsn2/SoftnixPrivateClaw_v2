import asyncio
import time

from claw.config import SandboxSettings
from claw.core.subagent import SubagentManager
from claw.core.turn_context import current_turn_deadline
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.db.stores import LLMConfigStore
from claw.providers.base import ChatResult, ProviderError, TextDelta, ToolCall
from claw.tools.spawn import SpawnTool
from tests.conftest import FakeProvider, text_turn


def _sandbox() -> EphemeralSandbox:
    return EphemeralSandbox(SandboxSettings(enabled=False))


async def test_subagent_returns_final_text(tmp_path):
    provider = FakeProvider([text_turn("research complete: 42")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    result = await mgr.run("find the answer")
    assert result == "research complete: 42"
    # The subagent must run in isolation — its first call carries only its own system+task.
    first_call = provider.calls[0]
    assert first_call[0]["role"] == "system"
    assert "subagent" in first_call[0]["content"].lower()
    assert first_call[1]["content"].startswith("find the answer")


async def test_subagent_uses_control_plane_fallback(db_factory, tmp_path):
    store = LLMConfigStore(db_factory)
    primary_provider = await store.create_provider("primary", "primary-key", "", model_prefix="openai")
    fallback_provider = await store.create_provider("fallback", "fallback-key", "", model_prefix="openai")
    await store.create_model(
        primary_provider.id, "openai/primary", "Primary", True, "low", "", kind="chat"
    )
    backup = await store.create_model(
        fallback_provider.id, "openai/backup", "Backup", True, "low", "", kind="chat"
    )
    await store.update_model(backup.id, owner_id=None, is_fallback=True)

    class FailingPrimary(FakeProvider):
        def __init__(self):
            super().__init__([])
            self.models = []

        async def stream_chat(self, messages, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"] == "openai/primary":
                raise ProviderError("provider unavailable")
            yield TextDelta(text="completed by backup")
            yield ChatResult(content="completed by backup")

    provider = FailingPrimary()
    manager = SubagentManager(
        provider,
        _sandbox(),
        tmp_path,
        model="openai/primary",
        max_iterations=5,
        llm_config=store,
    )

    assert await manager.run("finish this") == "completed by backup"
    assert provider.models == ["openai/primary", "openai/backup"]

async def test_subagent_can_use_tools(tmp_path):
    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[
                        ToolCall(id="1", name="write_file", arguments={"path": "out.txt", "content": "hi"})
                    ],
                )
            ],
            text_turn("wrote the file"),
        ]
    )
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    result = await mgr.run("create out.txt")
    assert result == "wrote the file"
    assert (tmp_path / "out.txt").read_text() == "hi"


async def test_spawn_tool_delegates(tmp_path):
    provider = FakeProvider([text_turn("done by subagent")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    tool = SpawnTool(mgr)
    assert await tool.execute(task="do a thing") == "done by subagent"


class SlowProvider(FakeProvider):
    async def stream_chat(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        async for event in super().stream_chat(*args, **kwargs):
            yield event


async def test_subagent_that_runs_out_of_time_says_so_rather_than_blaming_the_step_limit(tmp_path):
    provider = SlowProvider(
        [
            [
                ChatResult(
                    content=None, tool_calls=[ToolCall(id="1", name="list_dir", arguments={"path": "."})]
                )
            ],
            text_turn("never reached"),
        ]
    )
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5, max_turn_seconds=0.01)
    result = await mgr.run("take too long")
    assert "ran out of time" in result
    assert "step limit" not in result


async def test_subagent_is_clamped_to_the_parent_turns_deadline(tmp_path, monkeypatch):
    # The parent loop only checks its budget between iterations, so a spawn that
    # granted itself a fresh 600s would keep the whole turn alive for a second
    # full budget. The subagent must inherit whatever time is left instead.
    from claw.core import subagent as subagent_mod

    real = subagent_mod.AgentLoop
    seen: dict[str, float] = {}

    def spy(**kwargs):
        seen["budget"] = kwargs["max_turn_seconds"]
        return real(**kwargs)

    monkeypatch.setattr(subagent_mod, "AgentLoop", spy)
    provider = FakeProvider([text_turn("ok")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5, max_turn_seconds=600)

    token = current_turn_deadline.set(time.monotonic() + 5)
    try:
        await mgr.run("do a thing")
    finally:
        current_turn_deadline.reset(token)

    assert 0 < seen["budget"] <= 5


async def test_subagent_refuses_to_start_once_the_parent_turn_is_over(tmp_path):
    provider = FakeProvider([text_turn("should never run")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    token = current_turn_deadline.set(time.monotonic() - 1)
    try:
        result = await mgr.run("too late")
    finally:
        current_turn_deadline.reset(token)
    assert "out of time" in result
    assert provider.calls == []  # never reached the provider at all


async def test_subagent_context_appended(tmp_path):
    provider = FakeProvider([text_turn("ok")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    await mgr.run("summarize", context="the source material")
    user_msg = provider.calls[0][1]["content"]
    assert "summarize" in user_msg and "the source material" in user_msg
