"""Playwright-backed PageSource/PageDriver. Imported lazily — only when browser
automation is enabled — so the rest of the app never depends on Playwright."""

from typing import Any

from loguru import logger

from claw.config import BrowserSettings


class PlaywrightPageDriver:
    def __init__(self, context: Any, page: Any, timeout_ms: int):
        self._context = context
        self._page = page
        self._timeout = timeout_ms

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout)

    async def text(self) -> str:
        try:
            return await self._page.inner_text("body", timeout=self._timeout)
        except Exception:
            return await self._page.content()

    async def title(self) -> str:
        return await self._page.title()

    async def url(self) -> str:
        return self._page.url

    async def click(self, selector: str) -> None:
        await self._page.click(selector, timeout=self._timeout)
        await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout)

    async def fill(self, selector: str, value: str) -> None:
        await self._page.fill(selector, value, timeout=self._timeout)

    async def links(self, limit: int = 40) -> list[dict[str, str]]:
        raw = await self._page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.innerText, href: e.href})).filter(l => l.href)",
        )
        return raw[:limit]

    async def screenshot(self, path: str) -> None:
        await self._page.screenshot(path=path, full_page=False)

    async def close(self) -> None:
        await self._context.close()


class PlaywrightBrowser:
    """Owns a single shared Chromium; hands out one isolated context+page per call."""

    def __init__(self, settings: BrowserSettings):
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = None  # created lazily to avoid importing asyncio at import time

    async def _ensure(self) -> None:
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.settings.headless)
            logger.info("Playwright Chromium launched (headless={})", self.settings.headless)

    async def new_page(self) -> PlaywrightPageDriver:
        await self._ensure()
        context = await self._browser.new_context()
        page = await context.new_page()
        return PlaywrightPageDriver(context, page, self.settings.timeout_seconds * 1000)

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
