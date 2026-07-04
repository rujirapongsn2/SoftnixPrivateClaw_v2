"""Web tools: fetch a URL as readable text, search via Brave (optional)."""

import re
from typing import Any

import httpx

from claw.tools.base import Tool

_MAX_FETCH_CHARS = 30_000
_TAG_RE = re.compile(r"<(script|style)[\s\S]*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"\n{3,}|[ \t]{2,}")


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
        if not self.brave_api_key:
            return "Error: web search is not configured (missing Brave API key)"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": min(count, 10)},
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
