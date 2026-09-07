"""Publish an immutable copy of an existing deliverable."""

import asyncio
import shutil
import uuid
from typing import ClassVar

from sbot.filenames import safe_filename
from sbot.tools.filesystem import _WorkspaceTool


class PublishArtifactTool(_WorkspaceTool):
    name = "publish_artifact"
    description = (
        "Attach an existing workspace file for the user to download or preview. "
        "Call for every requested deliverable, including existing files requested again "
        "and source files. Publishes an immutable copy; does not overwrite the original."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    wants_progress = True

    async def execute(self, path: str, progress=None, **kwargs) -> str:
        source = self._resolve(path)
        if not source.is_file() or source.stat().st_size == 0:
            return "Error: deliverable must be an existing, nonempty file."
        if source.stat().st_size > 100 * 1024 * 1024:
            return "Error: deliverable exceeds the 100 MB publication limit."
        relative = f".deliveries/{uuid.uuid4().hex}/{safe_filename(source.name)}"
        destination = self._resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, source, destination)
        if progress:
            progress({"kind": "artifact_published", "path": relative})
        return f"Published downloadable file: {relative}"
