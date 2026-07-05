"""GitHub MCP server for the built-in GitHub connector preset."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

GITHUB_API_BASE_DEFAULT = "https://api.github.com"
GITHUB_USER_AGENT = "nanobot-github-connector/1.0"


@dataclass(frozen=True)
class GitHubClient:
    """Small GitHub REST API client used by the MCP server and validation flow."""

    token: str
    api_base: str = GITHUB_API_BASE_DEFAULT
    default_repo: str | None = None
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": GITHUB_USER_AGENT,
            },
            timeout=20.0,
            transport=self.transport,
        )

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise ValueError("GitHub token is required")
        with self._client() as client:
            response = client.request(method, path, params=params)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    def _resolve_repo(self, repo: str | None = None) -> str:
        resolved = str(repo or self.default_repo or _discover_repo_from_git() or "").strip()
        if not resolved or "/" not in resolved:
            raise ValueError(
                "GitHub repository must be provided as owner/repo. Set GITHUB_DEFAULT_REPO or run from a git repo with a GitHub origin."
            )
        owner, name = (part.strip() for part in resolved.split("/", 1))
        if not owner or not name:
            raise ValueError(
                "GitHub repository must be provided as owner/repo. Set GITHUB_DEFAULT_REPO or run from a git repo with a GitHub origin."
            )
        return f"{owner}/{name}"

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/user")

    def get_repository(self, repo: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self._resolve_repo(repo)}")

    def list_issues(
        self,
        repo: str | None = None,
        *,
        state: str = "open",
        per_page: int = 10,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/repos/{self._resolve_repo(repo)}/issues",
            params={"state": state, "per_page": per_page, "page": page},
        )
        return [item for item in payload if isinstance(item, dict)]

    def get_issue(self, number: int, repo: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self._resolve_repo(repo)}/issues/{int(number)}")

    def get_pull_request(self, number: int, repo: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self._resolve_repo(repo)}/pulls/{int(number)}")

    def list_workflow_runs(
        self,
        repo: str | None = None,
        *,
        per_page: int = 10,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/repos/{self._resolve_repo(repo)}/actions/runs",
            params={"per_page": per_page, "page": page},
        )
        runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        return [item for item in runs if isinstance(item, dict)]

    def list_commits(
        self,
        repo: str | None = None,
        *,
        per_page: int = 10,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/repos/{self._resolve_repo(repo)}/commits",
            params={"per_page": per_page, "page": page},
        )
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def get_latest_commit(self, repo: str | None = None) -> dict[str, Any]:
        commits = self.list_commits(repo=repo, per_page=1, page=1)
        return commits[0] if commits else {}

    def search_repositories(self, query: str, *, per_page: int = 10, page: int = 1) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/search/repositories",
            params={"q": str(query or "").strip(), "per_page": per_page, "page": page},
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]


def _client_from_env() -> GitHubClient:
    return GitHubClient(
        token=str(os.environ.get("GITHUB_TOKEN") or "").strip(),
        api_base=str(os.environ.get("GITHUB_API_BASE") or GITHUB_API_BASE_DEFAULT).strip() or GITHUB_API_BASE_DEFAULT,
        default_repo=str(os.environ.get("GITHUB_DEFAULT_REPO") or "").strip() or None,
    )


def _connector_context() -> dict[str, Any]:
    default_repo = str(os.environ.get("GITHUB_DEFAULT_REPO") or "").strip() or None
    inferred_repo = _discover_repo_from_git()
    return {
        "api_base": str(os.environ.get("GITHUB_API_BASE") or GITHUB_API_BASE_DEFAULT).strip() or GITHUB_API_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("GITHUB_TOKEN") or "").strip()),
        "default_repo": default_repo,
        "inferred_repo": inferred_repo,
        "effective_repo": default_repo or inferred_repo,
    }


@lru_cache(maxsize=1)
def _discover_repo_from_git() -> str | None:
    """Best-effort discover owner/repo from the current git checkout."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    remote = str(result.stdout or "").strip()
    if not remote:
        return None
    remote = remote.removesuffix(".git")
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    elif remote.startswith("ssh://git@github.com/"):
        remote = remote.removeprefix("ssh://git@github.com/")
    if remote.count("/") >= 1:
        parts = remote.split("/")
        owner, name = parts[-2], parts[-1]
        if owner and name:
            return f"{owner}/{name}"
    return None


mcp = FastMCP(
    "github-connector",
    instructions=(
        "GitHub connector for repository, issue, pull request, workflow, and repository search tasks. "
        "Use the tools for structured GitHub access instead of ad-hoc web scraping."
    ),
)


@mcp.tool(description="Return the authenticated GitHub user for token validation.")
def whoami() -> dict[str, Any]:
    client = _client_from_env()
    return client.whoami()


@mcp.tool(description="Get a repository by owner/repo. If repo is omitted, use the configured default repo or inferred git remote.")
def get_repository(repo: str | None = None) -> dict[str, Any]:
    client = _client_from_env()
    return client.get_repository(repo=repo)


@mcp.tool(description="List repository issues. If repo is omitted, use the configured default repo or inferred git remote.")
def list_issues(repo: str | None = None, state: str = "open", per_page: int = 10, page: int = 1) -> list[dict[str, Any]]:
    client = _client_from_env()
    return client.list_issues(repo=repo, state=state, per_page=per_page, page=page)


@mcp.tool(description="Get one repository issue by number. If repo is omitted, use the configured default repo or inferred git remote.")
def get_issue(number: int, repo: str | None = None) -> dict[str, Any]:
    client = _client_from_env()
    return client.get_issue(number=number, repo=repo)


@mcp.tool(description="Get one pull request by number. If repo is omitted, use the configured default repo or inferred git remote.")
def get_pull_request(number: int, repo: str | None = None) -> dict[str, Any]:
    client = _client_from_env()
    return client.get_pull_request(number=number, repo=repo)


@mcp.tool(description="List workflow runs for a repository. If repo is omitted, use the configured default repo or inferred git remote.")
def list_workflow_runs(repo: str | None = None, per_page: int = 10, page: int = 1) -> list[dict[str, Any]]:
    client = _client_from_env()
    return client.list_workflow_runs(repo=repo, per_page=per_page, page=page)


@mcp.tool(description="List commits for a repository. If repo is omitted, use the configured default repo or inferred git remote.")
def list_commits(repo: str | None = None, per_page: int = 10, page: int = 1) -> list[dict[str, Any]]:
    client = _client_from_env()
    return client.list_commits(repo=repo, per_page=per_page, page=page)


@mcp.tool(description="Get the latest commit for a repository. If repo is omitted, use the configured default repo or inferred git remote.")
def get_latest_commit(repo: str | None = None) -> dict[str, Any]:
    client = _client_from_env()
    return client.get_latest_commit(repo=repo)


@mcp.tool(description="Search GitHub repositories by query.")
def search_repositories(query: str, per_page: int = 10, page: int = 1) -> list[dict[str, Any]]:
    client = _client_from_env()
    return client.search_repositories(query, per_page=per_page, page=page)


@mcp.tool(description="Return the GitHub connector runtime context, including configured default repo and effective repo selection.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
