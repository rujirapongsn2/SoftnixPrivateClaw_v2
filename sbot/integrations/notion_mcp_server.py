"""Notion MCP server for the built-in Notion connector preset."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

NOTION_API_BASE_DEFAULT = "https://api.notion.com/v1"
NOTION_VERSION_DEFAULT = "2026-03-11"
NOTION_USER_AGENT = "nanobot-notion-connector/1.0"
_NOTION_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)([0-9a-f]{32})"),
    re.compile(r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
)


def normalize_notion_target_id(value: str | None) -> str | None:
    """Extract a Notion page/database ID from a raw ID or a Notion URL."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    candidate = raw
    if parsed.scheme and parsed.netloc:
        candidate = parsed.path.rstrip("/").split("/")[-1] or raw
    candidate = candidate.split("?")[0].split("#")[0]
    for pattern in _NOTION_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1).replace("-", "").lower()
    if parsed.scheme and parsed.netloc:
        fallback = candidate.replace("-", "").strip()
        if fallback:
            return fallback.lower()
    return candidate.lower()


@dataclass(frozen=True)
class NotionClient:
    """Small Notion REST API client used by the MCP server and validation flow."""

    token: str
    api_base: str = NOTION_API_BASE_DEFAULT
    notion_version: str = NOTION_VERSION_DEFAULT
    default_page_id: str | None = None
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.notion_version,
                "User-Agent": NOTION_USER_AGENT,
            },
            timeout=20.0,
            transport=self.transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        if not self.token:
            raise ValueError("Notion token is required")
        with self._client() as client:
            response = client.request(method, path, params=params, json=json)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    def _resolve_page_id(self, page_id: str | None = None) -> str:
        resolved = normalize_notion_target_id(page_id or self.default_page_id)
        if not resolved:
            raise ValueError("Notion page ID is required. Set NOTION_DEFAULT_PAGE_ID or provide page_id.")
        return resolved

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/users/me")

    def search(self, query: str = "", *, filter_object: str | None = None, page_size: int = 10) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": str(query or "").strip(), "page_size": int(page_size)}
        if filter_object:
            payload["filter"] = {"property": "object", "value": str(filter_object).strip()}
        return self._request("POST", "/search", json=payload)

    def get_page(self, page_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/pages/{self._resolve_page_id(page_id)}")

    def get_block_children(self, block_id: str | None = None, *, page_size: int = 100) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/blocks/{self._resolve_page_id(block_id)}/children",
            params={"page_size": int(page_size)},
        )

    def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/data-sources/{str(data_source_id).strip()}")

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{str(database_id).strip()}")


def _client_from_env() -> NotionClient:
    return NotionClient(
        token=str(os.environ.get("NOTION_TOKEN") or "").strip(),
        api_base=str(os.environ.get("NOTION_API_BASE") or NOTION_API_BASE_DEFAULT).strip() or NOTION_API_BASE_DEFAULT,
        notion_version=str(os.environ.get("NOTION_VERSION") or NOTION_VERSION_DEFAULT).strip() or NOTION_VERSION_DEFAULT,
        default_page_id=str(os.environ.get("NOTION_DEFAULT_PAGE_ID") or "").strip() or None,
    )


def _connector_context() -> dict[str, Any]:
    default_page_id = str(os.environ.get("NOTION_DEFAULT_PAGE_ID") or "").strip() or None
    return {
        "api_base": str(os.environ.get("NOTION_API_BASE") or NOTION_API_BASE_DEFAULT).strip() or NOTION_API_BASE_DEFAULT,
        "notion_version": str(os.environ.get("NOTION_VERSION") or NOTION_VERSION_DEFAULT).strip() or NOTION_VERSION_DEFAULT,
        "has_token": bool(str(os.environ.get("NOTION_TOKEN") or "").strip()),
        "default_page_id": default_page_id,
        "effective_page_id": normalize_notion_target_id(default_page_id),
    }


mcp = FastMCP(
    "notion-connector",
    instructions=(
        "Notion connector for workspace search, page retrieval, and block content reading tasks. "
        "Use the tools for structured Notion access instead of ad-hoc scraping."
    ),
)


@mcp.tool(description="Return the authenticated Notion bot user for token validation.")
def whoami() -> dict[str, Any]:
    return _client_from_env().whoami()


@mcp.tool(description="Search Notion pages and data sources by title or query text.")
def search(query: str = "", filter_object: str | None = None, page_size: int = 10) -> dict[str, Any]:
    return _client_from_env().search(query=query, filter_object=filter_object, page_size=page_size)


@mcp.tool(description="Get a Notion page. If page_id is omitted, use the configured default page ID.")
def get_page(page_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_page(page_id=page_id)


@mcp.tool(description="Get the child blocks for a Notion page or block. If block_id is omitted, use the configured default page ID.")
def get_block_children(block_id: str | None = None, page_size: int = 100) -> dict[str, Any]:
    return _client_from_env().get_block_children(block_id=block_id, page_size=page_size)


@mcp.tool(description="Get a Notion data source by ID.")
def get_data_source(data_source_id: str) -> dict[str, Any]:
    return _client_from_env().get_data_source(data_source_id=data_source_id)


@mcp.tool(description="Get a Notion database by ID.")
def get_database(database_id: str) -> dict[str, Any]:
    return _client_from_env().get_database(database_id=database_id)


@mcp.tool(description="Return the Notion connector runtime context, including configured default page ID.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
