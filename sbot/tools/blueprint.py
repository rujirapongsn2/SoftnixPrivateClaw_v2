"""Tool for saving an agent-created workspace file as a reusable Blueprint."""

import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Any

from sbot.tools.base import Tool
from sbot.filenames import safe_filename


class SaveBlueprintTool(Tool):
    name = "save_blueprint"
    description = (
        "Save an existing DOCX, XLSX, or PPTX file from the user's workspace as a reusable "
        "Blueprint. Use this when the user asks you to save a file or template as a Blueprint. "
        "Default to private; use group or public only when the user explicitly asks to share it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path of the completed file."},
            "name": {"type": "string", "description": "Short name shown in the Blueprint library."},
            "description": {"type": "string", "description": "Optional purpose or usage note."},
            "visibility": {"type": "string", "enum": ["private", "group", "public"]},
        },
        "required": ["path", "name"],
    }

    _SUPPORTED = {".docx", ".xlsx", ".pptx"}
    _MAX_BYTES = 50 * 1024 * 1024

    def __init__(self, store: Any, root: Path, workspace: Path, user_id: str):
        self.store = store
        self.root = root
        self.workspace = workspace
        self.user_id = user_id

    async def execute(
        self,
        path: str,
        name: str,
        description: str = "",
        visibility: str = "private",
        **_: Any,
    ) -> str:
        try:
            source = (self.workspace.resolve() / path).resolve()
            source.relative_to(self.workspace.resolve())
        except (OSError, ValueError):
            return "Error: file must be inside the user's workspace."
        if not source.is_file():
            return f"Error: file not found: {path}"
        suffix = source.suffix.lower()
        if suffix not in self._SUPPORTED:
            return "Error: Blueprints support DOCX, XLSX, and PPTX files."
        size = source.stat().st_size
        if size > self._MAX_BYTES:
            return "Error: Blueprint exceeds the 50 MB limit."
        title = str(name or "").strip()
        if not title:
            return "Error: Blueprint name is required."
        if visibility not in {"private", "group", "public"}:
            visibility = "private"

        blueprint_id = uuid.uuid4().hex
        filename = safe_filename(source.name, fallback='blueprint')
        storage_path = f"{blueprint_id}/v1/{filename}"
        destination = self.root / storage_path
        destination.parent.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copy2(source, destination)
            await self.store.create(
                blueprint_id=blueprint_id,
                owner_id=self.user_id,
                name=title,
                description=str(description or ""),
                visibility=visibility,
                filename=filename,
                mime=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                size=size,
                storage_path=storage_path,
            )
        except Exception as exc:
            shutil.rmtree(self.root / blueprint_id, ignore_errors=True)
            return f"Error: could not save Blueprint: {exc}"
        return f"Saved Blueprint '{title}' as version 1 ({visibility})."
