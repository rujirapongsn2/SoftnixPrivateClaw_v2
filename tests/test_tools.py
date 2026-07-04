from typing import Any

from claw.tools.base import Tool
from claw.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from claw.tools.registry import ToolRegistry


class StrictTool(Tool):
    name = "strict"
    description = "Requires an integer"
    parameters = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }

    async def execute(self, n: int, **_: Any) -> str:
        return str(n * 2)


async def test_registry_validates_params():
    registry = ToolRegistry()
    registry.register(StrictTool())
    result = await registry.execute("strict", {"n": "not-a-number"})
    assert result.startswith("Error: invalid parameters")
    assert await registry.execute("strict", {"n": 21}) == "42"


async def test_registry_audit_hook_called():
    seen = []
    registry = ToolRegistry(on_execute=lambda name, params, result: seen.append((name, result)))
    registry.register(StrictTool())
    await registry.execute("strict", {"n": 1})
    assert seen == [("strict", "2")]


async def test_filesystem_roundtrip_and_escape_protection(tmp_path):
    ws = tmp_path / "ws"
    write, read, ls = WriteFileTool(ws), ReadFileTool(ws), ListDirTool(ws)

    await write.execute(path="notes/a.txt", content="hello")
    assert await read.execute(path="notes/a.txt") == "hello"
    listing = await ls.execute(path="notes")
    assert "a.txt" in listing

    registry = ToolRegistry()
    registry.register(read)
    escaped = await registry.execute("read_file", {"path": "../../etc/passwd"})
    assert escaped.startswith("Error")
