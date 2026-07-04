"""Browser tool: drive a live, server-side browser for pages web_fetch can't handle
(logged-in sites, JavaScript apps, multi-step interactions)."""

from typing import Any

from claw.browser.manager import BrowserManager
from claw.tools.base import Tool


class BrowserTool(Tool):
    name = "browser"
    description = (
        "Control a live web browser for pages that need real rendering or interaction: "
        "logged-in sites, single-page apps, forms, or anything web_fetch cannot read. "
        "Actions: navigate (open a URL), read (current page text), click (a CSS selector), "
        "fill (a selector with a value), links (list clickable links), screenshot. "
        "Call navigate first, then read/click/fill to work through the page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "read", "click", "fill", "links", "screenshot"],
            },
            "url": {"type": "string", "description": "For navigate"},
            "selector": {"type": "string", "description": "CSS selector for click/fill"},
            "value": {"type": "string", "description": "Value for fill"},
        },
        "required": ["action"],
    }

    def __init__(self, manager: BrowserManager, user_id: str):
        self.manager = manager
        self.user_id = user_id

    async def execute(self, action: str, **kwargs: Any) -> str:
        return await self.manager.execute(self.user_id, action, kwargs)
