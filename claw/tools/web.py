"""Web tools: fetch a URL as readable text, search the web.

Web search defaults to DuckDuckGo's keyless HTML endpoint (no API key, no extra
dependency — just httpx), so search works out of the box. If a Brave API key is
configured it's preferred instead, since Brave's API returns higher-quality,
structured results.
"""

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from claw.tools.base import Tool

_MAX_FETCH_CHARS = 30_000
_TAG_RE = re.compile(r"<(script|style)[\s\S]*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"\n{3,}|[ \t]{2,}")

# Mimic a real browser — DuckDuckGo's HTML endpoint returns an empty page to
# obvious bots/unknown user agents.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
_DDG_RESULT_RE = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET_RE = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    from html import unescape

    return re.sub(r"\s+", " ", unescape(_STRIP_TAGS_RE.sub("", s))).strip()


def _unwrap_ddg_url(url: str) -> str:
    """DuckDuckGo wraps result links in a /l/?uddg=<encoded-url> redirect."""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return url


def _html_to_text(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _WS_RE.sub("\n", text).strip()


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a URL and return its readable text content."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "http(s) URL"}},
        "required": ["url"],
    }

    async def execute(self, url: str, **_: Any) -> str:
        if not re.match(r"^https?://", url):
            return "Error: only http(s) URLs are supported"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers={"User-Agent": "ClawAgent/0.1"})
        content_type = resp.headers.get("content-type", "")
        body = resp.text
        text = _html_to_text(body) if "html" in content_type else body
        if len(text) > _MAX_FETCH_CHARS:
            text = text[:_MAX_FETCH_CHARS] + "\n... (truncated)"
        return f"[{resp.status_code}] {url}\n\n{text}"


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return top results with titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer", "description": "Number of results (default 5)"},
        },
        "required": ["query"],
    }

    def __init__(self, brave_api_key: str = ""):
        self.brave_api_key = brave_api_key

    async def execute(self, query: str, count: int = 5, **_: Any) -> str:
        count = max(1, min(count, 10))
        if self.brave_api_key:
            return await self._search_brave(query, count)
        return await self._search_duckduckgo(query, count)

    async def _search_duckduckgo(self, query: str, count: int) -> str:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                    headers={"User-Agent": _BROWSER_UA},
                )
        except httpx.HTTPError as e:
            return f"Error: web search request failed ({e})"
        if resp.status_code != 200:
            return f"Error: web search failed with status {resp.status_code}"
        titles = _DDG_RESULT_RE.findall(resp.text)
        snippets = _DDG_SNIPPET_RE.findall(resp.text)
        lines = []
        for i, (url, title) in enumerate(titles[:count]):
            snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
            lines.append(f"{i + 1}. {_strip_tags(title)}\n   {_unwrap_ddg_url(url)}\n   {snippet}")
        return "\n".join(lines) or "No results."

    async def _search_brave(self, query: str, count: int) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={"X-Subscription-Token": self.brave_api_key},
            )
        if resp.status_code != 200:
            return f"Error: search failed with status {resp.status_code}"
        results = (resp.json().get("web") or {}).get("results") or []
        lines = [
            f"{i + 1}. {r.get('title', '')}\n   {r.get('url', '')}\n   {r.get('description', '')}"
            for i, r in enumerate(results)
        ]
        return "\n".join(lines) or "No results."
