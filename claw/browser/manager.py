"""Per-user browser session manager and action dispatch.

Each user gets one page (isolated browser context) from the PageSource, launched
lazily on first use. The manager serializes actions per user and returns
model-friendly text results.
"""

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any

from loguru import logger

from claw.browser.base import PageDriver, PageSource
from claw.config import BrowserSettings

_ACTIONS = ("navigate", "read", "click", "fill", "links", "screenshot")


class BrowserManager:
    def __init__(self, source: PageSource, settings: BrowserSettings, workspace_root: Path):
        self.source = source
        self.settings = settings
        self.workspace_root = workspace_root
        self._pages: dict[str, PageDriver] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _page(self, user_id: str) -> PageDriver:
        page = self._pages.get(user_id)
        if page is None:
            page = await self.source.new_page()
            self._pages[user_id] = page
        return page

    async def execute(self, user_id: str, action: str, args: dict[str, Any]) -> str:
        if action not in _ACTIONS:
            return f"Error: unknown browser action '{action}'. Valid: {', '.join(_ACTIONS)}"
        async with self._locks[user_id]:
            try:
                page = await self._page(user_id)
                return await self._dispatch(user_id, page, action, args)
            except Exception as exc:
                logger.warning("Browser action {} failed for {}: {}", action, user_id, exc)
                return f"Error: browser {action} failed: {exc}"

    async def _dispatch(self, user_id: str, page: PageDriver, action: str, args: dict[str, Any]) -> str:
        if action == "navigate":
            url = str(args.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url.lstrip("/") if url else ""
            if not url:
                return "Error: navigate requires a url"
            await page.goto(url)
            return await self._describe(page)

        if action == "read":
            return await self._describe(page, include_links=False)

        if action == "click":
            selector = str(args.get("selector") or "").strip()
            if not selector:
                return "Error: click requires a selector"
            await page.click(selector)
            return await self._describe(page)

        if action == "fill":
            selector = str(args.get("selector") or "").strip()
            value = str(args.get("value") or "")
            if not selector:
                return "Error: fill requires a selector"
            await page.fill(selector, value)
            return f"Filled {selector}."

        if action == "links":
            links = await page.links(limit=40)
            if not links:
                return "No links found on the current page."
            return "\n".join(f"- {l.get('text', '').strip()[:60]} → {l.get('href', '')}" for l in links)

        if action == "screenshot":
            out_dir = self.workspace_root / user_id / "browser"
            out_dir.mkdir(parents=True, exist_ok=True)
            existing = len(list(out_dir.glob("shot-*.png")))
            path = out_dir / f"shot-{existing + 1}.png"
            await page.screenshot(str(path))
            return f"Screenshot saved to browser/{path.name}"

        return f"Error: unhandled action {action}"

    async def _describe(self, page: PageDriver, *, include_links: bool = True) -> str:
        title = await page.title()
        url = await page.url()
        text = (await page.text())[: self.settings.max_chars]
        parts = [f"Title: {title}", f"URL: {url}", "", text]
        if include_links:
            links = await page.links(limit=15)
            if links:
                parts.append("\nKey links:")
                parts += [f"- {l.get('text', '').strip()[:50]} → {l.get('href', '')}" for l in links]
        return "\n".join(parts)

    async def close_user(self, user_id: str) -> None:
        page = self._pages.pop(user_id, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                logger.debug("Closing page for {} raised; ignored", user_id)

    async def close(self) -> None:
        for user_id in list(self._pages):
            await self.close_user(user_id)
        try:
            await self.source.close()
        except Exception:
            logger.debug("Closing browser source raised; ignored")
