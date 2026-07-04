from pathlib import Path

from claw.config import SandboxSettings
from claw.core.subagent import SubagentManager
from claw.sandbox.ephemeral import EphemeralSandbox
from claw.providers.base import ChatResult, ToolCall
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


async def test_subagent_can_use_tools(tmp_path):
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[ToolCall(id="1", name="write_file",
                                                           arguments={"path": "out.txt", "content": "hi"})])],
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


async def test_subagent_context_appended(tmp_path):
    provider = FakeProvider([text_turn("ok")])
    mgr = SubagentManager(provider, _sandbox(), tmp_path, max_iterations=5)
    await mgr.run("summarize", context="the source material")
    user_msg = provider.calls[0][1]["content"]
    assert "summarize" in user_msg and "the source material" in user_msg
