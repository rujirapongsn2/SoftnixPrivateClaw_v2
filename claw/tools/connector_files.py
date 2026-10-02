"""Copy a file a connector holds into the workspace without routing it through the model.

Some MCP servers (fframes renders, for example) hand out finished files only as
base64 chunks over the MCP connection: `artifact_info` describes the file
(size, SHA-256, name, chunk size) and `read_artifact_chunk` returns one slice at
a time. A model cannot move such a file itself: it would have to repeat every
base64 character back in a write_file call (a 400 KB video is ~550K characters
of output). This tool runs that loop in-process, decodes and verifies the bytes,
and saves the file in the workspace, where it shows up as a downloadable file.

Only the calling user's connected tools are reachable (it looks them up in that
user's own registry at call time), only the two read operations of the named
connector are called, and the result is size-capped and checksum-verified
before it replaces anything in the workspace.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from claw.tools.base import Tool
from claw.tools.registry import ToolRegistry

_INFO_TOOL = "artifact_info"
_DEFAULT_CHUNK_TOOL = "read_artifact_chunk"
# A server may name its own chunk reader, but it has to look like one: a read
# operation of the same connector, never an arbitrary write/submit tool.
_CHUNK_TOOL_RE = re.compile(r"^read_[a-z0-9_]*chunk[a-z0-9_]*$")
_DEFAULT_CHUNK_BYTES = 32 * 1024
_MAX_CHUNK_BYTES = 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_DIR = "downloads"


class SaveConnectorFileTool(Tool):
    name = "save_connector_file"
    description = (
        "Save a file held by a connected MCP server (e.g. a rendered video) into the workspace "
        "so the user can download it. Use this instead of reading base64 chunks yourself or "
        "fetching private download URLs. Works with connectors that expose `artifact_info` and "
        "`read_artifact_chunk`; the file is checksum-verified and shown to the user as a file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "connector": {
                "type": "string",
                "description": "Connector name: the part between mcp_ and the tool name (mcp_<connector>_artifact_info)",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments that identify the file for artifact_info, e.g. {\"job_id\": \"...\", \"file\": \"video.mp4\"}",
            },
            "save_as": {
                "type": "string",
                "description": "Optional workspace-relative path; defaults to downloads/<server filename>",
            },
        },
        "required": ["connector", "arguments"],
    }
    wants_deadline = True
    # Tells the agent loop to look for new workspace files after this tool, as
    # it does after `exec`, so the saved file is offered to the user as a file.
    writes_workspace = True

    def __init__(
        self,
        registry: ToolRegistry,
        workspace: Path,
        max_bytes: int,
        quota: Callable[[int], Awaitable[str | None]] | None = None,
    ):
        self.registry = registry
        self.workspace = workspace.resolve()
        self.max_bytes = max_bytes
        # Called with the file's size before any byte is fetched; a non-empty
        # answer (the workspace is full) is returned to the model instead.
        self.quota = quota

    def offered(self) -> bool:
        """Advertised only while some connected server describes files this way."""
        return any(
            name.startswith("mcp_") and name.endswith(f"_{_INFO_TOOL}") for name in self.registry.tool_names
        )

    def _connector_tool(self, connector: str, operation: str) -> Tool | None:
        return self.registry.get(f"mcp_{connector}_{operation}")

    def _target(self, save_as: str, filename: str) -> Path:
        if save_as:
            raw = Path(save_as)
            target = (raw if raw.is_absolute() else self.workspace / raw).resolve()
        else:
            safe = _SAFE_NAME_RE.sub("_", PurePosixPath(filename or "download.bin").name).strip("._") or "download.bin"
            target = (self.workspace / _DEFAULT_DIR / safe).resolve()
        if not target.is_relative_to(self.workspace) or target == self.workspace:
            raise ValueError(f"save_as escapes the workspace: {save_as}")
        return target

    async def execute(
        self, connector: str, arguments: dict, save_as: str = "", deadline: float | None = None, **_: Any
    ) -> str:
        connector = str(connector or "").strip()
        if connector.startswith("mcp_"):
            connector = connector[4:]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", connector):
            return "Error: connector must be a connector name such as 'fframs'"
        if not isinstance(arguments, dict) or not all(
            isinstance(v, (str, int, float, bool)) for v in arguments.values()
        ):
            return "Error: arguments must be a flat object of the identifiers artifact_info takes"
        info_tool = self._connector_tool(connector, _INFO_TOOL)
        if info_tool is None:
            return (
                f"Error: connector '{connector}' is not connected or has no {_INFO_TOOL} tool; "
                "this tool only works with connectors that serve files in chunks"
            )
        try:
            info = json.loads(await self.registry.execute(info_tool.name, dict(arguments), deadline=deadline))
        except (TypeError, ValueError):
            return f"Error: mcp_{connector}_{_INFO_TOOL} did not return file details (JSON)"
        if not isinstance(info, dict):
            return f"Error: mcp_{connector}_{_INFO_TOOL} did not return file details (JSON)"
        size = info.get("size_bytes")
        expected_sha = str(info.get("sha256") or "").lower()
        if not isinstance(size, int) or size < 0:
            return f"Error: mcp_{connector}_{_INFO_TOOL} did not report size_bytes"
        server_cap = info.get("max_file_bytes")
        cap = min(self.max_bytes, server_cap) if isinstance(server_cap, int) and server_cap > 0 else self.max_bytes
        if size > cap:
            return f"Error: file is {size} bytes, over the {cap}-byte download limit"
        if self.quota is not None:
            full = await self.quota(size)
            if full:
                return full
        if str(info.get("encoding") or "base64").lower() != "base64":
            return f"Error: unsupported chunk encoding {info.get('encoding')!r}"
        chunk_name = str(info.get("chunk_tool") or _DEFAULT_CHUNK_TOOL)
        if not _CHUNK_TOOL_RE.match(chunk_name):
            return f"Error: refusing chunk tool {chunk_name!r}: not a chunk read operation"
        chunk_tool = self._connector_tool(connector, chunk_name)
        if chunk_tool is None:
            return f"Error: connector '{connector}' has no {chunk_name} tool"
        chunk_bytes = info.get("max_chunk_bytes")
        chunk_bytes = (
            min(chunk_bytes, _MAX_CHUNK_BYTES) if isinstance(chunk_bytes, int) and chunk_bytes > 0 else _DEFAULT_CHUNK_BYTES
        )
        try:
            target = self._target(save_as, str(info.get("filename") or arguments.get("file") or ""))
        except ValueError as exc:
            return f"Error: {exc}"

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.part")
        digest = hashlib.sha256()
        received = 0
        offset = 0
        # The server's own next_offset drives the loop; this bound stops a server
        # that never reports eof from spinning.
        # Servers may return smaller slices than they advertise, so allow for
        # slices as small as 1 KiB; an empty slice that is not the end is an error.
        max_calls = size // 1024 + 3
        try:
            with partial.open("wb") as out:
                for _call in range(max_calls):
                    if deadline is not None and time.monotonic() >= deadline:
                        return "Error: the turn ran out of time while saving the file; nothing was saved"
                    # One audit entry per saved file (the save_connector_file call itself),
                    # not one per 32 KB chunk: a 100 MB file would otherwise write ~3,000.
                    raw = await self.registry.execute(
                        chunk_tool.name,
                        {**arguments, "offset": offset, "length": chunk_bytes},
                        deadline=deadline,
                        audit=False,
                    )
                    if str(raw).startswith("Error"):
                        return f"Error: reading chunk at offset {offset} failed: {str(raw)[:200]}"
                    try:
                        chunk = json.loads(raw)
                        data = base64.b64decode(str(chunk.get("data_base64") or ""), validate=True)
                    except (TypeError, ValueError, AttributeError, binascii.Error):
                        return f"Error: chunk at offset {offset} was not valid base64 JSON"
                    if not data and not chunk.get("eof"):
                        return f"Error: server returned an empty chunk at offset {offset}"
                    received += len(data)
                    if received > size:
                        return f"Error: server sent more than the reported {size} bytes"
                    await asyncio.to_thread(out.write, data)
                    digest.update(data)
                    next_offset = chunk.get("next_offset")
                    if chunk.get("eof") or next_offset is None:
                        break
                    if not isinstance(next_offset, int) or next_offset <= offset:
                        return f"Error: server returned an invalid next_offset after {offset}"
                    offset = next_offset
                else:
                    return "Error: server never reported the end of the file"
            if received != size:
                return f"Error: received {received} bytes, expected {size}; nothing was saved"
            actual_sha = digest.hexdigest()
            if expected_sha and actual_sha != expected_sha:
                return "Error: SHA-256 mismatch; the download was discarded"
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        relative = target.relative_to(self.workspace).as_posix()
        verified = "SHA-256 verified" if expected_sha else "no checksum reported"
        return f"Saved {relative} ({size} bytes, {info.get('mime_type') or 'unknown type'}, {verified})"
