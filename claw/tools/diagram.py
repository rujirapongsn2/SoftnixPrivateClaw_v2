"""Shared static diagram export tool for both runtimes."""

import json
from pathlib import Path

from loguru import logger

from claw.tools.base import Tool

_FALLBACK = "Publish the HTML/SVG source with publish_artifact instead."


class RenderDiagramTool(Tool):
    name = "render_diagram"
    description = "Validate a static HTML/SVG diagram and export PNG. Scripts/network are disabled. Use inline SVG/CSS and system fonts. Publish the returned PNG and source with publish_artifact."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "width": {"type": "integer", "minimum": 320, "maximum": 2400},
            "height": {"type": "integer", "minimum": 240, "maximum": 2400},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path, user_id: str | None = None):
        self.workspace = workspace
        self.user_id = user_id

    async def execute(self, path: str, width: int = 1440, height: int = 1000, **kwargs):
        from claw.skills.render import RenderRejected, render_diagram

        try:
            return json.dumps(await render_diagram(self.workspace, path, width, height, user_id=self.user_id))
        except RenderRejected as exc:
            # Problems the model can fix; these messages are ours and safe.
            return f"Error: PNG rendering failed: {exc}. {_FALLBACK}"
        except Exception as exc:
            # Worker stderr carries tracebacks and host paths; keep it in the log.
            logger.warning("render_diagram failed for {}: {!r}", path, exc)
            if isinstance(exc, TimeoutError):
                reason = "the renderer timed out"
            elif "Executable doesn't exist" in str(exc):
                reason = "the PNG renderer is not installed on this server"
            else:
                reason = "the PNG renderer is unavailable"
            return f"Error: PNG rendering failed ({reason}). {_FALLBACK}"
