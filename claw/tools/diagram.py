"""Shared static diagram export tool for both runtimes."""

import json
from pathlib import Path
from claw.tools.base import Tool


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
        from claw.skills.render import render_diagram

        try:
            return json.dumps(await render_diagram(self.workspace, path, width, height, user_id=self.user_id))
        except Exception as exc:
            return f"Error: PNG rendering failed ({type(exc).__name__}): {exc}. The HTML/SVG source can still be published."
