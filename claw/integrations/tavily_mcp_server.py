"""Tavily MCP server for the built-in Tavily connector preset."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

TAVILY_API_BASE_DEFAULT = "https://api.tavily.com"
TAVILY_USER_AGENT = "nanobot-tavily-connector/1.0"


def _normalize_optional_list(values: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if values is None:
        return None
    cleaned = [str(item or "").strip() for item in values if str(item or "").strip()]
    return cleaned or None


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


@dataclass(frozen=True)
class TavilyClient:
    """Small Tavily REST API client used by the MCP server and validation flow."""

    api_key: str
    api_base: str = TAVILY_API_BASE_DEFAULT
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": TAVILY_USER_AGENT,
            },
            timeout=30.0,
            transport=self.transport,
        )

    def _request(self, path: str, payload: dict[str, Any]) -> Any:
        if not self.api_key:
            raise ValueError("Tavily API key is required")
        with self._client() as client:
            response = client.post(path, json={key: value for key, value in payload.items() if value is not None})
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    def search(
        self,
        query: str,
        *,
        search_depth: str = "basic",
        topic: str = "general",
        max_results: int = 5,
        include_answer: bool = False,
        include_raw_content: str | bool | None = None,
        include_images: bool = False,
        include_favicon: bool = False,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        days: int | None = None,
        time_range: str | None = None,
    ) -> dict[str, Any]:
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            raise ValueError("Search query is required")
        depth = str(search_depth or "basic").strip().lower() or "basic"
        if depth not in {"basic", "advanced"}:
            depth = "basic"
        cleaned_topic = str(topic or "general").strip().lower() or "general"
        payload: dict[str, Any] = {
            "query": cleaned_query,
            "search_depth": depth,
            "topic": cleaned_topic,
            "max_results": _clamp_int(max_results, minimum=1, maximum=20),
            "include_answer": bool(include_answer),
            "include_raw_content": include_raw_content,
            "include_images": bool(include_images),
            "include_favicon": bool(include_favicon),
            "include_domains": _normalize_optional_list(include_domains),
            "exclude_domains": _normalize_optional_list(exclude_domains),
            "days": int(days) if days is not None else None,
            "time_range": str(time_range or "").strip() or None,
        }
        return self._request("/search", payload)

    def extract(
        self,
        urls: str | list[str],
        *,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_images: bool = False,
        include_favicon: bool = False,
        include_usage: bool = False,
        timeout: float | None = None,
        query: str | None = None,
        chunks_per_source: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(urls, str):
            normalized_urls = [urls]
        else:
            normalized_urls = list(urls or [])
        cleaned_urls = [str(url or "").strip() for url in normalized_urls if str(url or "").strip()]
        if not cleaned_urls:
            raise ValueError("At least one URL is required")
        depth = str(extract_depth or "basic").strip().lower() or "basic"
        if depth not in {"basic", "advanced"}:
            depth = "basic"
        output_format = str(format or "markdown").strip().lower() or "markdown"
        if output_format not in {"markdown", "text"}:
            output_format = "markdown"
        payload: dict[str, Any] = {
            "urls": cleaned_urls[0] if len(cleaned_urls) == 1 else cleaned_urls,
            "extract_depth": depth,
            "format": output_format,
            "include_images": bool(include_images),
            "include_favicon": bool(include_favicon),
            "include_usage": bool(include_usage),
            "timeout": float(timeout) if timeout is not None else None,
            "query": str(query or "").strip() or None,
            "chunks_per_source": _clamp_int(chunks_per_source, minimum=1, maximum=5) if chunks_per_source is not None else None,
        }
        return self._request("/extract", payload)


def _client_from_env() -> TavilyClient:
    return TavilyClient(
        api_key=str(os.environ.get("TAVILY_API_KEY") or "").strip(),
        api_base=str(os.environ.get("TAVILY_API_BASE") or TAVILY_API_BASE_DEFAULT).strip() or TAVILY_API_BASE_DEFAULT,
    )


def _connector_context() -> dict[str, Any]:
    return {
        "api_base": str(os.environ.get("TAVILY_API_BASE") or TAVILY_API_BASE_DEFAULT).strip() or TAVILY_API_BASE_DEFAULT,
        "has_api_key": bool(str(os.environ.get("TAVILY_API_KEY") or "").strip()),
    }


mcp = FastMCP(
    "tavily-connector",
    instructions=(
        "Tavily connector for internet search and web page content extraction. "
        "Use it when the user needs current web information, source discovery, or page extraction."
    ),
)


@mcp.tool(description="Search the internet with Tavily and return ranked results, optional answer text, URLs, snippets, and metadata.")
def search(
    query: str,
    search_depth: str = "basic",
    topic: str = "general",
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: str | bool | None = None,
    include_images: bool = False,
    include_favicon: bool = False,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    days: int | None = None,
    time_range: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().search(
        query=query,
        search_depth=search_depth,
        topic=topic,
        max_results=max_results,
        include_answer=include_answer,
        include_raw_content=include_raw_content,
        include_images=include_images,
        include_favicon=include_favicon,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        days=days,
        time_range=time_range,
    )


@mcp.tool(description="Extract clean page content from one URL or a list of URLs using Tavily Extract.")
def extract(
    urls: str | list[str],
    extract_depth: str = "basic",
    format: str = "markdown",
    include_images: bool = False,
    include_favicon: bool = False,
    include_usage: bool = False,
    timeout: float | None = None,
    query: str | None = None,
    chunks_per_source: int | None = None,
) -> dict[str, Any]:
    return _client_from_env().extract(
        urls=urls,
        extract_depth=extract_depth,
        format=format,
        include_images=include_images,
        include_favicon=include_favicon,
        include_usage=include_usage,
        timeout=timeout,
        query=query,
        chunks_per_source=chunks_per_source,
    )


@mcp.tool(description="Return the Tavily connector runtime context without exposing the API key.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
