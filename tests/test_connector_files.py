"""save_connector_file streams a connector's chunked file into the workspace."""

import base64
import hashlib
import json
import time
from typing import Any

from claw.providers.base import ChatResult, ToolCall
from claw.tools.base import Tool
from claw.tools.connector_files import SaveConnectorFileTool
from claw.tools.registry import ToolRegistry
from tests.test_artifact_jobs import make_runtime
from tests.conftest import FakeProvider

PAYLOAD = bytes(range(256)) * 300  # 76,800 bytes: three 32 KiB chunks


class FakeInfo(Tool):
    name = "mcp_demo_artifact_info"
    description = "info"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, payload: bytes, **overrides: Any):
        self.payload = payload
        self.overrides = overrides

    async def execute(self, **kwargs: Any) -> str:
        return json.dumps({
            "job_id": kwargs.get("job_id"), "file": kwargs.get("file"),
            "filename": "fframes-job.mp4", "mime_type": "video/mp4",
            "size_bytes": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest(),
            "encoding": "base64", "max_chunk_bytes": 32768, "max_file_bytes": 33554432,
            "chunk_tool": "read_artifact_chunk", **self.overrides,
        })


class FakeChunks(Tool):
    name = "mcp_demo_read_artifact_chunk"
    description = "chunk"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, payload: bytes, *, never_eof: bool = False):
        self.payload = payload
        self.never_eof = never_eof
        self.calls: list[dict] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        offset, length = kwargs["offset"], kwargs["length"]
        # A server that never ends trickles one byte per call and never says eof.
        data = self.payload[offset:offset + (1 if self.never_eof else length)]
        end = offset + len(data)
        eof = end >= len(self.payload) and not self.never_eof
        return json.dumps({
            "data_base64": base64.b64encode(data).decode(),
            "next_offset": None if eof else end,
            "eof": eof,
        })


class SmallSliceChunks(FakeChunks):
    """Returns 1 KiB slices although the file advertises 32 KiB chunks."""

    async def execute(self, **kwargs: Any) -> str:
        return await super().execute(**{**kwargs, "length": min(kwargs["length"], 1024)})


class EmptySliceChunks(FakeChunks):
    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps({"data_base64": "", "next_offset": kwargs["offset"] + 1, "eof": False})


def _tool(tmp_path, payload=PAYLOAD, max_bytes=10_000_000, info=None, chunks=None):
    registry = ToolRegistry()
    registry.register(info or FakeInfo(payload))
    chunk_tool = chunks or FakeChunks(payload)
    registry.register(chunk_tool)
    tool = SaveConnectorFileTool(registry, tmp_path, max_bytes)
    registry.register(tool)
    return tool, chunk_tool, registry


async def test_saves_verified_file_without_model_relay(tmp_path):
    tool, chunks, _ = _tool(tmp_path)
    result = await tool.execute(connector="demo", arguments={"job_id": "j1", "file": "video.mp4"})
    assert result.startswith("Saved downloads/fframes-job.mp4 (76800 bytes, video/mp4, SHA-256 verified)")
    assert (tmp_path / "downloads" / "fframes-job.mp4").read_bytes() == PAYLOAD
    assert len(chunks.calls) == 3
    assert all(call["job_id"] == "j1" and call["file"] == "video.mp4" for call in chunks.calls)
    assert not list((tmp_path / "downloads").glob(".*.part"))


async def test_checksum_mismatch_saves_nothing(tmp_path):
    tool, _, _ = _tool(tmp_path, info=FakeInfo(PAYLOAD, sha256="0" * 64))
    result = await tool.execute(connector="mcp_demo", arguments={"job_id": "j1", "file": "video.mp4"})
    assert result.startswith("Error: SHA-256 mismatch")
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_limits_and_refusals(tmp_path):
    tool, _, _ = _tool(tmp_path, max_bytes=1000)
    assert "download limit" in await tool.execute(connector="demo", arguments={"job_id": "j"})

    tool, _, _ = _tool(tmp_path)
    escape = await tool.execute(connector="demo", arguments={"job_id": "j"}, save_as="../outside.mp4")
    assert escape.startswith("Error: save_as escapes the workspace")
    assert not (tmp_path.parent / "outside.mp4").exists()

    tool, _, _ = _tool(tmp_path, info=FakeInfo(PAYLOAD, chunk_tool="submit_render"))
    assert "not a chunk read operation" in await tool.execute(connector="demo", arguments={"job_id": "j"})

    assert "not connected" in await tool.execute(connector="other", arguments={"job_id": "j"})
    assert "flat object" in await tool.execute(connector="demo", arguments={"nested": {"x": 1}})


async def test_server_that_never_ends_is_bounded(tmp_path):
    chunks = FakeChunks(PAYLOAD, never_eof=True)
    tool, _, _ = _tool(tmp_path, chunks=chunks)
    result = await tool.execute(connector="demo", arguments={"job_id": "j"})
    assert result == "Error: server never reported the end of the file"
    assert len(chunks.calls) == len(PAYLOAD) // 1024 + 3
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_expired_deadline_stops_before_reading(tmp_path):
    tool, chunks, _ = _tool(tmp_path)
    result = await tool.execute(connector="demo", arguments={"job_id": "j"}, deadline=time.monotonic() - 1)
    assert "ran out of time" in result
    assert chunks.calls == []


def test_only_advertised_when_a_connector_serves_files(tmp_path):
    registry = ToolRegistry()
    tool = SaveConnectorFileTool(registry, tmp_path, 1000)
    registry.register(tool)
    assert "save_connector_file" not in {d["function"]["name"] for d in registry.get_definitions()}
    registry.register(FakeInfo(PAYLOAD))
    assert "save_connector_file" in {d["function"]["name"] for d in registry.get_definitions()}


async def test_saved_file_is_offered_as_an_artifact(stores, db_factory, tmp_path):
    call = ToolCall(
        id="save-1", name="save_connector_file",
        arguments={"connector": "demo", "arguments": {"job_id": "j1", "file": "video.mp4"}},
    )
    provider = FakeProvider([[ChatResult(content=None, tool_calls=[call])], [ChatResult(content="Your video is ready")]])
    runtime, _ = make_runtime(stores, db_factory, provider, tmp_path, max_turn_seconds=30)
    user = await stores["users"].get_or_create_by_email("connector-file@example.com")
    session = await stores["sessions"].create(user.id)
    agent = runtime.get_agent(user.id)
    agent.tools.register(FakeInfo(PAYLOAD))
    agent.tools.register(FakeChunks(PAYLOAD))

    assert await runtime.handle_message(user.id, session.id, "Send me the finished clip") == "Your video is ready"

    history = await stores["messages"].recent(session.id)
    artifacts = [p for m in history for p in (m.get("meta") or {}).get("artifacts", [])]
    assert artifacts == ["downloads/fframes-job.mp4"]
    assert (agent.workspace / "downloads" / "fframes-job.mp4").read_bytes() == PAYLOAD
    await runtime.drain()


async def test_server_returning_small_slices_still_completes(tmp_path):
    chunks = SmallSliceChunks(PAYLOAD)
    tool, _, _ = _tool(tmp_path, chunks=chunks)
    result = await tool.execute(connector="demo", arguments={"job_id": "j"})
    assert result.startswith("Saved downloads/fframes-job.mp4")
    assert (tmp_path / "downloads" / "fframes-job.mp4").read_bytes() == PAYLOAD
    assert len(chunks.calls) == len(PAYLOAD) // 1024


async def test_empty_slice_that_is_not_the_end_is_an_error(tmp_path):
    tool, chunks, _ = _tool(tmp_path, chunks=EmptySliceChunks(PAYLOAD))
    result = await tool.execute(connector="demo", arguments={"job_id": "j"})
    assert "empty chunk at offset 0" in result
    assert len(chunks.calls) == 1


async def test_chunks_are_not_each_audited(tmp_path):
    audited: list[str] = []
    registry = ToolRegistry(on_execute=lambda name, params, result: audited.append(name))
    registry.register(FakeInfo(PAYLOAD))
    registry.register(FakeChunks(PAYLOAD))
    tool = SaveConnectorFileTool(registry, tmp_path, 10_000_000)
    await tool.execute(connector="demo", arguments={"job_id": "j"})
    # The file's details are audited once; its chunks (hundreds for a big file) are not,
    # because the save_connector_file call that wraps them is audited itself.
    assert audited == ["mcp_demo_artifact_info"]
