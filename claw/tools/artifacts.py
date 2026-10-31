"""Publish an immutable copy of an existing deliverable."""

import asyncio
import hashlib
import os
import tempfile
import shutil
from typing import ClassVar

from claw.filenames import safe_filename
from claw.tools.filesystem import _WorkspaceTool


def _sha256(path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
        digest = await asyncio.to_thread(_sha256, source)
        if content_hash is not None and digest != content_hash:
            return 'Error: artifact changed after verification.'
        # Keyed by content, so publishing the same file again reuses one copy
        # instead of growing .deliveries/ on every request.
        relative = f".deliveries/{digest}/{safe_filename(source.name)}"
        destination = self._resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)

        def publish_once():
            # Atomic create, no overwrite. Retries/restarts of an identical
            # immutable deliverable retain the same public reference.
            with tempfile.NamedTemporaryFile(dir=destination.parent) as temporary:
                shutil.copyfile(source, temporary.name)
                try:
                    os.link(temporary.name, destination)
                except FileExistsError:
                    pass
            if _sha256(destination) != digest:
                raise ValueError('publication hash collision or modified artifact')
        await asyncio.to_thread(publish_once)
        if progress:
            progress({"kind": "artifact_published", "path": relative})
        return f"Published downloadable file: {relative}"
