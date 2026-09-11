"""GitHub MCP server for the built-in GitHub connector preset."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import quote

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

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: dict | None = None) -> Any:
        if not self.token:
            raise ValueError("GitHub token is required")
        with self._client() as client:
            response = client.request(method, path, params=params, json=json)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    def _resolve_repo(self, repo: str | None = None) -> str:
        resolved = str(repo or self.default_repo or _discover_repo_from_git() or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", resolved):
            raise ValueError("GitHub repository must be owner/repo")
        return resolved

    def _write_repo(self, repo: str | None) -> str:
        # Never infer a write target from the connector process's own checkout.
        # That may be Sbot's repository, not the user's application.
        target = repo or self.default_repo
        if not target:
            raise ValueError("writes require an explicit repo or configured GITHUB_DEFAULT_REPO")
        return self._resolve_repo(target)

    def publish_files(self, branch: str, message: str, files: dict[str, str],
                      repo: str | None = None, base: str = "main",
                      expected_sha: str | None = None) -> dict[str, Any]:
        """One atomic tree commit on a feature branch, without persisting a token
        in an agent workspace. Non-force ref updates reject divergent writers.
        On retry compare returned SHA before publishing again.
        """
        if not branch or branch == base or branch in {"main", "master"}:
            raise ValueError("publish to a feature branch, not the base branch")
        if not message.strip() or not files or len(files) > 100:
            raise ValueError("provide a commit message and 1–100 text files")
        if any(not isinstance(v, str) for v in files.values()):
            raise TypeError("files must map paths to text content")
        if sum(len(v.encode()) for v in files.values()) > 1_000_000:
            raise ValueError("publish at most 1 MB of text per commit")
        for path, content in files.items():
            if (not path or path.startswith('/') or '\\' in path
                    or any(part in {'', '.', '..', '.git'} for part in path.split('/'))):
                raise ValueError("file paths must be repository-relative")
            if not isinstance(content, str):
                raise TypeError("files must map paths to text content")
        root = f"/repos/{self._write_repo(repo)}"
        ref_path = f"{root}/git/ref/heads/{quote(branch, safe='')}"
        exists = True
        try:
            head = self._request("GET", ref_path)["object"]["sha"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            exists = False
            head = self._request("GET", f"{root}/git/ref/heads/{quote(base, safe='')}")["object"]["sha"]
        if expected_sha is not None and head != expected_sha:
            raise ValueError("branch changed: read its latest commit before retrying")
        commit = self._request("GET", f"{root}/git/commits/{head}")
        tree = self._request("POST", f"{root}/git/trees", json={
            "base_tree": commit["tree"]["sha"],
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": content}
                     for path, content in files.items()],
        })
        new = self._request("POST", f"{root}/git/commits", json={
            "message": message, "tree": tree["sha"], "parents": [head],
        })
        if exists:
            self._request("PATCH", f"{root}/git/refs/heads/{quote(branch, safe='')}",
                          json={"sha": new["sha"], "force": False})
        else:
            self._request("POST", f"{root}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": new["sha"]})
        return {"sha": new["sha"], "branch": branch, "url": new.get("html_url", new.get("url"))}

    def create_pull_request(self, title: str, head: str, base: str = "main", body: str = "",
                            repo: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/repos/{self._write_repo(repo)}/pulls",
                             json={"title": title, "head": head, "base": base, "body": body, "draft": True})

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str] | None = None,
                          repo: str | None = None) -> dict[str, Any]:
        if not workflow or not ref:
            raise ValueError("workflow and ref are required")
        self._request("POST", f"/repos/{self._write_repo(repo)}/actions/workflows/{quote(workflow, safe='')}/dispatches",
                      json={"ref": ref, "inputs": inputs or {}})
        return {"status": "dispatched", "workflow": workflow, "ref": ref,
                "note": "Queued only. Check workflow runs and deployment health before reporting success."}

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



@mcp.tool(description="Publish 1–100 text files in one commit to a feature branch. Creates it from base if absent. "
          "Never force-pushes. Use expected_sha to detect stale work. Credentials stay in the connector.")
def publish_files(branch: str, message: str, files: dict[str, str], repo: str | None = None,
                  base: str = "main", expected_sha: str | None = None) -> dict[str, Any]:
    return _client_from_env().publish_files(branch, message, files, repo, base, expected_sha)


@mcp.tool(description="Create a draft pull request for a published feature branch.")
def create_pull_request(title: str, head: str, base: str = "main", body: str = "",
                        repo: str | None = None) -> dict[str, Any]:
    return _client_from_env().create_pull_request(title, head, base, body, repo)


@mcp.tool(description="Dispatch an existing GitHub Actions workflow on a ref. Requires workflow_dispatch. "
          "This queues work; inspect workflow runs and health before claiming deployment succeeded.")
def dispatch_workflow(workflow: str, ref: str, inputs: dict[str, str] | None = None,
                      repo: str | None = None) -> dict[str, Any]:
    return _client_from_env().dispatch_workflow(workflow, ref, inputs, repo)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
