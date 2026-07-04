"""Filesystem tools, restricted to the agent's workspace."""

import asyncio
from pathlib import Path
from typing import Any

from claw.tools.base import Tool

_MAX_READ_CHARS = 50_000


class _WorkspaceTool(Tool):
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(self, raw: str) -> Path:
        path = Path(raw)
        resolved = (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError(f"path escapes the workspace: {raw}")
        return resolved


class ReadFileTool(_WorkspaceTool):
    name = "read_file"
    description = "Read a text file from the workspace."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path relative to the workspace"}},
        "required": ["path"],
    }

    async def execute(self, path: str, **_: Any) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"Error: file not found: {path}"
        text = await asyncio.to_thread(target.read_text, "utf-8", "replace")
        if len(text) > _MAX_READ_CHARS:
            return text[:_MAX_READ_CHARS] + f"\n... (truncated, {len(text)} chars total)"
        return text


class WriteFileTool(_WorkspaceTool):
    name = "write_file"
    description = "Write content to a file in the workspace, creating parent directories."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, path: str, content: str, **_: Any) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, "utf-8")
        return f"Wrote {len(content)} chars to {path}"


class EditFileTool(_WorkspaceTool):
    name = "edit_file"
    description = "Replace an exact text fragment in a file. old_text must occur exactly once."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    async def execute(self, path: str, old_text: str, new_text: str, **_: Any) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"Error: file not found: {path}"
        text = await asyncio.to_thread(target.read_text, "utf-8")
        count = text.count(old_text)
        if count == 0:
            return "Error: old_text not found in file"
        if count > 1:
            return f"Error: old_text occurs {count} times; provide a unique fragment"
        await asyncio.to_thread(target.write_text, text.replace(old_text, new_text, 1), "utf-8")
        return f"Edited {path}"


class ListDirTool(_WorkspaceTool):
    name = "list_dir"
    description = "List files and directories at a workspace path."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Defaults to workspace root"}},
        "required": [],
    }

    async def execute(self, path: str = ".", **_: Any) -> str:
        target = self._resolve(path)
        if not target.is_dir():
            return f"Error: not a directory: {path}"
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [f"{'d' if e.is_dir() else 'f'} {e.relative_to(self.workspace)}" for e in entries[:500]]
        return "\n".join(lines) or "(empty)"
