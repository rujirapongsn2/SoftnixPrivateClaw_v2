"""Browser manager + tool tested with a fake page backend (no Chromium)."""

from pathlib import Path

from claw.browser.manager import BrowserManager
from claw.config import BrowserSettings
from claw.tools.browser import BrowserTool


class FakePage:
    def __init__(self):
        self._url = "about:blank"
        self._title = ""
        self._text = ""
        self.actions: list[tuple] = []
        self.closed = False

    async def goto(self, url):
        self.actions.append(("goto", url))
        self._url = url
        self._title = "Example Domain"
        self._text = "This domain is for use in illustrative examples."

    async def text(self):
        return self._text

    async def title(self):
        return self._title

    async def url(self):
        return self._url

    async def click(self, selector):
        self.actions.append(("click", selector))

    async def fill(self, selector, value):
        self.actions.append(("fill", selector, value))

    async def links(self, limit=40):
        return [{"text": "More", "href": "https://example.com/more"}][:limit]

    async def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG\r\n")

    async def close(self):
        self.closed = True


class FakeSource:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.closed = False

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


def _manager(tmp_path) -> tuple[BrowserManager, FakeSource]:
    source = FakeSource()
    mgr = BrowserManager(source, BrowserSettings(enabled=True), tmp_path)
    return mgr, source


async def test_navigate_returns_title_and_text(tmp_path):
    mgr, _ = _manager(tmp_path)
    out = await mgr.execute("u1", "navigate", {"url": "example.com"})
    assert "Title: Example Domain" in out
    assert "URL: https://example.com" in out
    assert "illustrative examples" in out


async def test_navigate_normalizes_bare_domain(tmp_path):
    mgr, source = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "example.com"})
    assert source.pages[0].actions[0] == ("goto", "https://example.com")


async def test_click_and_fill_dispatch(tmp_path):
    mgr, source = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "https://x.com"})
    await mgr.execute("u1", "fill", {"selector": "#q", "value": "hello"})
    out = await mgr.execute("u1", "click", {"selector": "#go"})
    assert ("fill", "#q", "hello") in source.pages[0].actions
    assert ("click", "#go") in source.pages[0].actions
    assert "Title:" in out


async def test_screenshot_writes_file(tmp_path):
    mgr, _ = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "https://x.com"})
    out = await mgr.execute("u1", "screenshot", {})
    assert "Screenshot saved" in out
    assert (tmp_path / "u1" / "browser" / "shot-1.png").is_file()


async def test_links_action(tmp_path):
    mgr, _ = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "https://x.com"})
    out = await mgr.execute("u1", "links", {})
    assert "https://example.com/more" in out


async def test_unknown_action_and_missing_args(tmp_path):
    mgr, _ = _manager(tmp_path)
    assert (await mgr.execute("u1", "teleport", {})).startswith("Error: unknown browser action")
    assert (await mgr.execute("u1", "navigate", {})).startswith("Error: navigate requires")
    await mgr.execute("u1", "navigate", {"url": "https://x.com"})
    assert (await mgr.execute("u1", "click", {})).startswith("Error: click requires")


async def test_page_reused_per_user_and_isolated_across_users(tmp_path):
    mgr, source = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "https://a.com"})
    await mgr.execute("u1", "read", {})
    await mgr.execute("u2", "navigate", {"url": "https://b.com"})
    # u1 reused one page; u2 got its own.
    assert len(source.pages) == 2


async def test_close_releases_pages_and_source(tmp_path):
    mgr, source = _manager(tmp_path)
    await mgr.execute("u1", "navigate", {"url": "https://x.com"})
    page = source.pages[0]
    await mgr.close()
    assert page.closed and source.closed


async def test_tool_delegates_to_manager(tmp_path):
    mgr, _ = _manager(tmp_path)
    tool = BrowserTool(mgr, "u1")
    out = await tool.execute(action="navigate", url="https://x.com")
    assert "Title:" in out
