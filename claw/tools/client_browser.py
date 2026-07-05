"""Unified browser tool: drive the user's own paired Chrome when it's online,
otherwise fall back to the server-side (Playwright) browser.

The client path enqueues a task on the file-backed broker and waits for the
paired extension to poll it, run it against a real tab, and post the result
back — the same working principle as the reference project.
"""

import asyncio
import json
from typing import Any

from claw.browser.broker import BrowserBrokerStore
from claw.browser.manager import BrowserManager
from claw.config import BrowserSettings
from claw.tools.base import Tool

# Actions the paired extension understands. The server-side fallback only
# implements a subset; the rest need an online extension.
_CLIENT_ACTIONS = [
    "open", "extract_page", "collect_pages", "fill", "click",
    "select", "scroll", "wait", "screenshot", "submit",
]
# client action -> server-side manager action (Playwright fallback).
_SERVER_MAP = {
    "open": "navigate",
    "extract_page": "read",
    "click": "click",
    "fill": "fill",
    "screenshot": "screenshot",
}


class ClientBrowserTool(Tool):
    name = "browser"
    description = (
        "Control a web browser for pages that need real rendering, login sessions, or "
        "interaction. Prefers the user's own paired Chrome (via the Softnix PrivateClaw "
        "browser extension) so it can act on logged-in sites; falls back to an isolated "
        "server-side browser when no paired browser is online. "
        "Actions: open (URL), extract_page (text+links), collect_pages (paginated), "
        "click, fill, select, scroll, wait, screenshot, submit. Call open first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _CLIENT_ACTIONS},
            "url": {"type": "string", "description": "For open"},
            "selector_or_label": {"type": "string", "description": "CSS selector or visible label"},
            "value": {
                "type": "string",
                "description": "Action value. screenshot: 'visible'|'full'. scroll: down/up/top/bottom/pixels.",
            },
            "fields": {"type": "object", "description": "For fill: {selector_or_label: value}"},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 20},
            "max_items": {"type": "integer", "minimum": 1, "maximum": 1000},
            "browser_session_id": {"type": "string", "description": "Reuse the same tab across steps"},
        },
        "required": ["action"],
    }

    def __init__(
        self,
        *,
        broker: BrowserBrokerStore,
        user_id: str,
        settings: BrowserSettings,
        server_manager: BrowserManager | None = None,
    ):
        self.broker = broker
        self.user_id = user_id
        self.settings = settings
        self.server_manager = server_manager

    async def execute(self, action: str, **kwargs: Any) -> str:
        prefer_client = (
            self.settings.client_extension_enabled
            and await asyncio.to_thread(self.broker.is_online, self.user_id)
        )
        if prefer_client:
            return await self._run_client(action, kwargs)
        if self.server_manager is not None:
            return await self._run_server(action, kwargs)
        if self.settings.client_extension_enabled:
            return (
                "No paired browser is online. Open Settings → Browser extension, install and pair "
                "the Softnix PrivateClaw extension, then retry."
            )
        return "Browser automation is not available on this server."

    async def _run_client(self, action: str, kwargs: dict[str, Any]) -> str:
        task_payload: dict[str, Any] = {"action": action, "user_id": self.user_id}
        for key in ("url", "selector_or_label", "value", "fields", "max_pages", "max_items", "browser_session_id"):
            if kwargs.get(key) is not None:
                task_payload[key] = kwargs[key]
        if action == "submit" and self.settings.require_confirmation_for_submit:
            task_payload["requires_confirmation"] = True

        task = await asyncio.to_thread(self.broker.enqueue_task, task_payload)
        task_id = str(task["task_id"])
        timeout = max(1, int(self.settings.poll_timeout_seconds or 60))
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await asyncio.to_thread(self.broker.read_result, task_id)
            if result is not None:
                return self._format(result)
            await asyncio.sleep(1)
        return f"The paired browser did not complete the task within {timeout}s."

    async def _run_server(self, action: str, kwargs: dict[str, Any]) -> str:
        mapped = _SERVER_MAP.get(action)
        if mapped is None:
            return (
                f"Action '{action}' needs an online paired browser extension. "
                "The server-side browser supports: open, extract_page, click, fill, screenshot."
            )
        # The server-side manager uses `selector` and `read`/`navigate` naming.
        args: dict[str, Any] = {}
        if kwargs.get("url"):
            args["url"] = kwargs["url"]
        if kwargs.get("selector_or_label"):
            args["selector"] = kwargs["selector_or_label"]
        if kwargs.get("value") is not None:
            args["value"] = kwargs["value"]
        return await self.server_manager.execute(self.user_id, mapped, args)

    @staticmethod
    def _format(result: dict[str, Any]) -> str:
        text = json.dumps(result, ensure_ascii=False)
        return text if len(text) <= 30_000 else text[:30_000] + "…(truncated)"
