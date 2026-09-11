"""Publish an immutable copy of an existing deliverable."""

import asyncio
import hashlib
import os
import tempfile
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

    async def execute(self, path: str, progress=None, content_hash=None, **kwargs) -> str:
        source = self._resolve(path)
        if not source.is_file() or source.stat().st_size == 0:
            return "Error: deliverable must be an existing, nonempty file."
        if source.stat().st_size > 100 * 1024 * 1024:
            return "Error: deliverable exceeds the 100 MB publication limit."
        if content_hash is not None:
            with source.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != content_hash:
                    return 'Error: artifact changed after verification.'
        relative = f".deliveries/{content_hash or uuid.uuid4().hex}/{safe_filename(source.name)}"
        destination = self._resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if content_hash is None:
            await asyncio.to_thread(shutil.copy2, source, destination)
        else:
            def publish_once():
                # Atomic create, no overwrite. Retries/restarts of an identical
                # immutable deliverable retain the same public reference.
                with tempfile.NamedTemporaryFile(dir=destination.parent) as temporary:
                    shutil.copyfile(source, temporary.name)
                    try:
                        os.link(temporary.name, destination)
                    except FileExistsError:
                        pass
                with destination.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != content_hash:
                        raise ValueError('publication hash collision or modified artifact')
            await asyncio.to_thread(publish_once)
        if progress:
            progress({"kind": "artifact_published", "path": relative})
        return f"Published downloadable file: {relative}"
