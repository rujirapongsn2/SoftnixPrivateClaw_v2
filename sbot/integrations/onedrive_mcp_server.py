"""OneDrive MCP server for the built-in OneDrive connector preset."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

ONEDRIVE_GRAPH_BASE_DEFAULT = "https://graph.microsoft.com/v1.0"
ONEDRIVE_USER_ID_DEFAULT = "me"
ONEDRIVE_TENANT_ID_DEFAULT = "organizations"
ONEDRIVE_USER_AGENT = "nanobot-onedrive-connector/1.0"
ONEDRIVE_WRITE_SCOPES = {"Files.ReadWrite", "Files.ReadWrite.All", "Sites.ReadWrite.All"}
ONEDRIVE_READ_SCOPES = {"Files.Read", "Files.Read.All", "Files.ReadWrite", "Files.ReadWrite.All", "Sites.Read.All", "Sites.ReadWrite.All"}


@dataclass
class OneDriveClient:
    """Small Microsoft Graph OneDrive client used by the MCP server and validation flow."""

    token: str
    graph_base: str = ONEDRIVE_GRAPH_BASE_DEFAULT
    user_id: str = ONEDRIVE_USER_ID_DEFAULT
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = ONEDRIVE_TENANT_ID_DEFAULT
    token_uri: str = ""
    scopes: str = ""
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": ONEDRIVE_USER_AGENT,
        }
        return httpx.Client(
            base_url=self.graph_base.rstrip("/"),
            headers=headers,
            timeout=20.0,
            transport=self.transport,
            follow_redirects=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        content: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        retry_on_auth_error: bool = True,
    ) -> Any:
        if not self.token:
            raise ValueError("OneDrive access token is required")
        with self._client() as client:
            response = client.request(
                method,
                path,
                params=params,
                json=json_data,
                content=content,
                headers=extra_headers,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if retry_on_auth_error and response.status_code == 401 and self._refresh_access_token():
                    return self._request(
                        method,
                        path,
                        params=params,
                        json_data=json_data,
                        content=content,
                        extra_headers=extra_headers,
                        retry_on_auth_error=False,
                    )
                detail = _extract_response_detail(response)
                if detail:
                    raise httpx.HTTPStatusError(
                        f"{exc}: {detail}",
                        request=exc.request,
                        response=exc.response,
                    ) from exc
                raise
            if not response.content:
                return {}
            if "application/json" in str(response.headers.get("content-type") or ""):
                return response.json()
            return {
                "content_base64": base64.b64encode(response.content).decode("ascii"),
                "size_bytes": len(response.content),
                "content_type": str(response.headers.get("content-type") or "application/octet-stream"),
            }

    def _request_url(self, url: str) -> dict[str, Any]:
        if not self.token:
            raise ValueError("OneDrive access token is required")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": ONEDRIVE_USER_AGENT,
        }
        with httpx.Client(
            timeout=20.0,
            transport=self.transport,
            headers=headers,
            follow_redirects=True,
        ) as client:
            response = client.get(str(url).strip())
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _extract_response_detail(response)
                if detail:
                    raise httpx.HTTPStatusError(
                        f"{exc}: {detail}",
                        request=exc.request,
                        response=exc.response,
                    ) from exc
                raise
            return response.json()

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        resolved = str(user_id or self.user_id or ONEDRIVE_USER_ID_DEFAULT).strip()
        return resolved or ONEDRIVE_USER_ID_DEFAULT

    def _user_path(self, user_id: str | None = None) -> str:
        resolved = self._resolve_user_id(user_id)
        if resolved.lower() == "me":
            return "/me"
        return f"/users/{resolved}"

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", self._user_path())

    def get_drive(self, *, user_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"{self._user_path(user_id)}/drive")

    def list_children(
        self,
        *,
        item_id: str | None = None,
        folder_path: str | None = None,
        top: int = 50,
        select: str | None = None,
        order_by: str | None = None,
        page_url: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if page_url:
            return self._request_url(page_url)
        params: dict[str, Any] = {"$top": int(top)}
        if select:
            params["$select"] = str(select).strip()
        if order_by:
            params["$orderby"] = str(order_by).strip()
        if folder_path:
            path = f"{self._user_path(user_id)}/drive/root:/{_encode_drive_path(folder_path)}:/children"
        elif item_id:
            path = f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}/children"
        else:
            path = f"{self._user_path(user_id)}/drive/root/children"
        return self._request("GET", path, params=params)

    def search(
        self,
        query: str,
        *,
        top: int = 25,
        select: str | None = None,
        page_url: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if page_url:
            return self._request_url(page_url)
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("OneDrive search query is required")
        params: dict[str, Any] = {"$top": int(top)}
        if select:
            params["$select"] = str(select).strip()
        escaped_query = normalized_query.replace("'", "''")
        path = f"{self._user_path(user_id)}/drive/root/search(q='{escaped_query}')"
        return self._request("GET", path, params=params)

    def get_item(
        self,
        item_id: str,
        *,
        select: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"$select": str(select).strip()} if str(select or "").strip() else None
        return self._request("GET", f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}", params=params)

    def get_item_by_path(
        self,
        item_path: str,
        *,
        select: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_path = _normalize_drive_path(item_path)
        if not normalized_path:
            raise ValueError("OneDrive item path is required")
        params = {"$select": str(select).strip()} if str(select or "").strip() else None
        return self._request("GET", f"{self._user_path(user_id)}/drive/root:/{_encode_drive_path(normalized_path)}", params=params)

    def find_file_exact(
        self,
        file_name: str,
        *,
        folder_path: str | None = None,
        top: int = 50,
        max_pages: int = 10,
        case_sensitive: bool = False,
        select: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = str(file_name or "").strip()
        if not normalized_name:
            raise ValueError("OneDrive file name is required")
        select_value = str(select or "id,name,size,lastModifiedDateTime,webUrl,parentReference,file,folder").strip()
        matches: list[dict[str, Any]] = []
        pages_scanned = 0
        next_link = ""

        if str(folder_path or "").strip():
            response = self.list_children(
                folder_path=folder_path,
                top=top,
                select=select_value,
                user_id=user_id,
            )
            while True:
                pages_scanned += 1
                matches.extend(_filter_exact_name(response.get("value", []), normalized_name, case_sensitive=case_sensitive))
                next_link = str(response.get("@odata.nextLink") or "")
                if not next_link or pages_scanned >= int(max_pages):
                    break
                response = self._request_url(next_link)
            strategy = "folder_children"
        else:
            response = self.search(
                normalized_name,
                top=top,
                select=select_value,
                user_id=user_id,
            )
            while True:
                pages_scanned += 1
                matches.extend(_filter_exact_name(response.get("value", []), normalized_name, case_sensitive=case_sensitive))
                next_link = str(response.get("@odata.nextLink") or "")
                if matches or not next_link or pages_scanned >= int(max_pages):
                    break
                response = self._request_url(next_link)
            strategy = "search_exact_filter"

        return {
            "query": normalized_name,
            "folder_path": _normalize_drive_path(folder_path) if str(folder_path or "").strip() else None,
            "matches": matches,
            "count": len(matches),
            "strategy": strategy,
            "pages_scanned": pages_scanned,
            "has_more": bool(next_link and pages_scanned >= int(max_pages)),
            "@odata.nextLink": next_link if next_link and pages_scanned >= int(max_pages) else None,
        }

    def download_file(
        self,
        item_id: str,
        *,
        max_bytes: int = 5_000_000,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        result = self._request("GET", f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}/content")
        size_bytes = int(result.get("size_bytes") or 0)
        if size_bytes > int(max_bytes):
            raise ValueError(f"OneDrive file is too large to return inline ({size_bytes} bytes > {int(max_bytes)} bytes)")
        return {
            "item_id": str(item_id).strip(),
            "content_base64": str(result.get("content_base64") or ""),
            "size_bytes": size_bytes,
            "content_type": str(result.get("content_type") or "application/octet-stream"),
        }

    def download_to_workspace(
        self,
        item_id: str,
        *,
        target_dir: str = "downloads/onedrive",
        file_name: str | None = None,
        conflict_behavior: str = "rename",
        max_bytes: int = 50_000_000,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        metadata = self.get_item(
            item_id,
            select="id,name,size,file,folder,lastModifiedDateTime,webUrl",
            user_id=user_id,
        )
        if metadata.get("folder") is not None:
            raise ValueError("OneDrive item is a folder, not a downloadable file")
        result = self._request("GET", f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}/content")
        payload = base64.b64decode(str(result.get("content_base64") or ""))
        size_bytes = len(payload)
        if size_bytes > int(max_bytes):
            raise ValueError(f"OneDrive file is too large to save ({size_bytes} bytes > {int(max_bytes)} bytes)")
        workspace = _workspace_root()
        destination_dir = _resolve_workspace_target_dir(workspace, target_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        resolved_name = _safe_file_name(file_name or metadata.get("name") or str(item_id).strip())
        destination = _resolve_conflict(destination_dir / resolved_name, conflict_behavior)
        destination.write_bytes(payload)
        relative = destination.relative_to(workspace).as_posix()
        return {
            "item_id": str(item_id).strip(),
            "name": str(metadata.get("name") or resolved_name),
            "local_path": relative,
            "absolute_path": str(destination),
            "size_bytes": size_bytes,
            "content_type": str(result.get("content_type") or mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "status": "saved",
        }

    def upload_file(
        self,
        *,
        local_path: str,
        target_path: str,
        conflict_behavior: str = "replace",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope()
        source = _resolve_local_file(local_path)
        data = source.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("OneDrive upload_file currently supports files up to 4 MB")
        normalized_target = str(target_path or "").strip().strip("/")
        if not normalized_target:
            normalized_target = source.name
        encoded_target = quote(normalized_target, safe="/")
        path = f"{self._user_path(user_id)}/drive/root:/{encoded_target}:/content"
        headers = {
            "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "Prefer": f'respond-async,handling=strict,@microsoft.graph.conflictBehavior="{str(conflict_behavior or "replace").strip()}"',
        }
        return self._request("PUT", path, content=data, extra_headers=headers)

    def create_folder(
        self,
        *,
        name: str,
        parent_item_id: str | None = None,
        conflict_behavior: str = "rename",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope()
        folder_name = str(name or "").strip()
        if not folder_name:
            raise ValueError("OneDrive folder name is required")
        payload = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": str(conflict_behavior or "rename").strip() or "rename",
        }
        if parent_item_id:
            path = f"{self._user_path(user_id)}/drive/items/{str(parent_item_id).strip()}/children"
        else:
            path = f"{self._user_path(user_id)}/drive/root/children"
        return self._request("POST", path, json_data=payload)

    def delete_item(self, item_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        self.ensure_write_scope()
        return self._request("DELETE", f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}")

    def create_share_link(
        self,
        item_id: str,
        *,
        link_type: str = "view",
        scope: str = "anonymous",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope()
        payload = {
            "type": str(link_type or "view").strip() or "view",
            "scope": str(scope or "anonymous").strip() or "anonymous",
        }
        return self._request("POST", f"{self._user_path(user_id)}/drive/items/{str(item_id).strip()}/createLink", json_data=payload)

    def token_scopes(self) -> set[str]:
        return _decode_access_token_permissions(self.token)

    def ensure_write_scope(self) -> set[str]:
        scopes = self.token_scopes()
        if scopes.intersection(ONEDRIVE_WRITE_SCOPES):
            return scopes
        raise ValueError(
            "OneDrive token does not include a Microsoft Graph file write scope. "
            "Regenerate the token with Files.ReadWrite."
        )

    def _refresh_access_token(self) -> bool:
        refresh_token = str(self.refresh_token or "").strip()
        client_id = str(self.client_id or "").strip()
        token_uri = self._resolved_token_uri()
        if not (refresh_token and client_id):
            return False
        form_data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        scopes = str(self.scopes or "").strip()
        if scopes:
            form_data["scope"] = scopes
        client_secret = str(self.client_secret or "").strip()
        if client_secret:
            form_data["client_secret"] = client_secret
        with httpx.Client(
            timeout=20.0,
            transport=self.transport,
            headers={"Accept": "application/json", "User-Agent": ONEDRIVE_USER_AGENT},
        ) as client:
            response = client.post(token_uri, data=form_data)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _extract_response_detail(response)
                if detail:
                    raise httpx.HTTPStatusError(
                        f"{exc}: {detail}",
                        request=exc.request,
                        response=exc.response,
                    ) from exc
                raise
            payload = response.json()
        new_token = str(payload.get("access_token") or "").strip()
        if not new_token:
            raise ValueError("OneDrive token refresh did not return an access token")
        self.token = new_token
        returned_refresh = str(payload.get("refresh_token") or "").strip()
        if returned_refresh:
            self.refresh_token = returned_refresh
        return True

    def _resolved_token_uri(self) -> str:
        explicit = str(self.token_uri or "").strip()
        if explicit:
            return explicit
        tenant = str(self.tenant_id or ONEDRIVE_TENANT_ID_DEFAULT).strip() or ONEDRIVE_TENANT_ID_DEFAULT
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def has_refresh_credentials(self) -> bool:
        return bool(str(self.refresh_token or "").strip() and str(self.client_id or "").strip())


def _decode_access_token_permissions(token: str) -> set[str]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return set()
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return set()
    permissions: set[str] = set()
    scopes = data.get("scp")
    if isinstance(scopes, str):
        permissions.update(item.strip() for item in scopes.split() if item.strip())
    roles = data.get("roles")
    if isinstance(roles, list):
        permissions.update(str(item).strip() for item in roles if str(item or "").strip())
    return permissions


def _extract_response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text.strip()
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            messages: list[str] = []
            message = str(error.get("message") or "").strip()
            if message:
                messages.append(message)
            inner = error.get("innerError")
            if isinstance(inner, dict):
                request_id = str(inner.get("request-id") or inner.get("requestId") or "").strip()
                if request_id:
                    messages.append(f"request-id {request_id}")
            return "; ".join(messages).strip()
        if isinstance(error, str):
            description = str(data.get("error_description") or "").strip()
            return f"{error}: {description}".strip(": ")
    return ""


def _resolve_local_file(local_path: str) -> Path:
    candidate = Path(str(local_path or "").strip()).expanduser()
    if candidate.is_absolute():
        path = candidate
    else:
        path = (Path.cwd() / candidate).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"OneDrive local file not found: {local_path}")
    return path


def _normalize_drive_path(path: str | None) -> str:
    return str(path or "").strip().strip("/")


def _encode_drive_path(path: str | None) -> str:
    return quote(_normalize_drive_path(path), safe="/")


def _filter_exact_name(items: Any, name: str, *, case_sensitive: bool) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    expected = name if case_sensitive else name.casefold()
    matches: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or "")
        candidate = item_name if case_sensitive else item_name.casefold()
        if candidate == expected:
            matches.append(item)
    return matches


def _workspace_root() -> Path:
    raw = str(os.environ.get("ONEDRIVE_WORKSPACE") or "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _resolve_workspace_target_dir(workspace: Path, target_dir: str) -> Path:
    raw = str(target_dir or "downloads/onedrive").strip() or "downloads/onedrive"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"OneDrive download target is outside workspace: {target_dir}") from exc
    return resolved


def _safe_file_name(file_name: str) -> str:
    name = Path(str(file_name or "").strip()).name
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")
    return safe or "onedrive-download"


def _resolve_conflict(path: Path, conflict_behavior: str) -> Path:
    behavior = str(conflict_behavior or "rename").strip().lower()
    if behavior not in {"rename", "replace", "fail"}:
        raise ValueError("conflict_behavior must be one of rename, replace, or fail")
    if behavior == "replace" or not path.exists():
        return path
    if behavior == "fail":
        raise FileExistsError(f"OneDrive download target already exists: {path}")
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find available OneDrive download target for: {path}")


def _client_from_env() -> OneDriveClient:
    tenant_id = str(os.environ.get("ONEDRIVE_TENANT_ID") or ONEDRIVE_TENANT_ID_DEFAULT).strip() or ONEDRIVE_TENANT_ID_DEFAULT
    return OneDriveClient(
        token=str(os.environ.get("ONEDRIVE_TOKEN") or "").strip(),
        graph_base=str(os.environ.get("ONEDRIVE_GRAPH_BASE") or ONEDRIVE_GRAPH_BASE_DEFAULT).strip() or ONEDRIVE_GRAPH_BASE_DEFAULT,
        user_id=str(os.environ.get("ONEDRIVE_USER_ID") or ONEDRIVE_USER_ID_DEFAULT).strip() or ONEDRIVE_USER_ID_DEFAULT,
        refresh_token=str(os.environ.get("ONEDRIVE_REFRESH_TOKEN") or "").strip(),
        client_id=str(os.environ.get("ONEDRIVE_CLIENT_ID") or "").strip(),
        client_secret=str(os.environ.get("ONEDRIVE_CLIENT_SECRET") or "").strip(),
        tenant_id=tenant_id,
        token_uri=str(os.environ.get("ONEDRIVE_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        scopes=str(os.environ.get("ONEDRIVE_SCOPES") or "").strip(),
    )


def _connector_context() -> dict[str, Any]:
    tenant_id = str(os.environ.get("ONEDRIVE_TENANT_ID") or ONEDRIVE_TENANT_ID_DEFAULT).strip() or ONEDRIVE_TENANT_ID_DEFAULT
    default_user_id = str(os.environ.get("ONEDRIVE_USER_ID") or ONEDRIVE_USER_ID_DEFAULT).strip() or ONEDRIVE_USER_ID_DEFAULT
    return {
        "graph_base": str(os.environ.get("ONEDRIVE_GRAPH_BASE") or ONEDRIVE_GRAPH_BASE_DEFAULT).strip() or ONEDRIVE_GRAPH_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("ONEDRIVE_TOKEN") or "").strip()),
        "has_refresh_token": bool(str(os.environ.get("ONEDRIVE_REFRESH_TOKEN") or "").strip()),
        "has_client_id": bool(str(os.environ.get("ONEDRIVE_CLIENT_ID") or "").strip()),
        "default_user_id": default_user_id,
        "effective_user_id": default_user_id,
        "tenant_id": tenant_id,
        "token_uri": str(os.environ.get("ONEDRIVE_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        "workspace": str(_workspace_root()),
        "capabilities": ["files.read", "files.write"],
    }


mcp = FastMCP(
    "onedrive-connector",
    instructions=(
        "OneDrive connector for drive inspection, file listing, file search, download/upload, "
        "folder creation, sharing, and delete tasks through Microsoft Graph."
    ),
)


@mcp.tool(description="Return the authenticated Microsoft Graph user profile for token validation.")
def whoami() -> dict[str, Any]:
    return _client_from_env().whoami()


@mcp.tool(description="Get OneDrive drive metadata for the configured user.")
def get_drive(user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_drive(user_id=user_id)


@mcp.tool(description="List OneDrive child items for root or a specific folder item.")
def list_children(
    item_id: str | None = None,
    folder_path: str | None = None,
    top: int = 50,
    select: str | None = None,
    order_by: str | None = None,
    page_url: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_children(
        item_id=item_id,
        folder_path=folder_path,
        top=top,
        select=select,
        order_by=order_by,
        page_url=page_url,
        user_id=user_id,
    )


@mcp.tool(description="Search OneDrive files and folders by query.")
def search(
    query: str,
    top: int = 25,
    select: str | None = None,
    page_url: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().search(query=query, top=top, select=select, page_url=page_url, user_id=user_id)


@mcp.tool(description="Get OneDrive item metadata by item ID.")
def get_item(item_id: str, select: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_item(item_id=item_id, select=select, user_id=user_id)


@mcp.tool(description="Get OneDrive item metadata by path such as 'temp/customer_subscription.xlsx'.")
def get_item_by_path(item_path: str, select: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_item_by_path(item_path=item_path, select=select, user_id=user_id)


@mcp.tool(description="Find OneDrive files by exact file name, optionally inside one folder path. Uses exact name matching and pagination.")
def find_file_exact(
    file_name: str,
    folder_path: str | None = None,
    top: int = 50,
    max_pages: int = 10,
    case_sensitive: bool = False,
    select: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().find_file_exact(
        file_name=file_name,
        folder_path=folder_path,
        top=top,
        max_pages=max_pages,
        case_sensitive=case_sensitive,
        select=select,
        user_id=user_id,
    )


@mcp.tool(description="Download OneDrive file content by item ID and return base64 payload.")
def download_file(item_id: str, max_bytes: int = 5_000_000, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().download_file(item_id=item_id, max_bytes=max_bytes, user_id=user_id)


@mcp.tool(description="Download one OneDrive file by item ID into the agent workspace and return the saved local path.")
def download_to_workspace(
    item_id: str,
    target_dir: str = "downloads/onedrive",
    file_name: str | None = None,
    conflict_behavior: str = "rename",
    max_bytes: int = 50_000_000,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().download_to_workspace(
        item_id=item_id,
        target_dir=target_dir,
        file_name=file_name,
        conflict_behavior=conflict_behavior,
        max_bytes=max_bytes,
        user_id=user_id,
    )


@mcp.tool(description="Upload one local file to OneDrive path. Files up to 4 MB are supported.")
def upload_file(
    local_path: str,
    target_path: str,
    conflict_behavior: str = "replace",
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().upload_file(
        local_path=local_path,
        target_path=target_path,
        conflict_behavior=conflict_behavior,
        user_id=user_id,
    )


@mcp.tool(description="Create one OneDrive folder under root or under one parent item.")
def create_folder(
    name: str,
    parent_item_id: str | None = None,
    conflict_behavior: str = "rename",
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_folder(
        name=name,
        parent_item_id=parent_item_id,
        conflict_behavior=conflict_behavior,
        user_id=user_id,
    )


@mcp.tool(description="Delete one OneDrive item by item ID.")
def delete_item(item_id: str, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().delete_item(item_id=item_id, user_id=user_id)


@mcp.tool(description="Create a OneDrive sharing link for one item.")
def create_share_link(
    item_id: str,
    link_type: str = "view",
    scope: str = "anonymous",
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_share_link(item_id=item_id, link_type=link_type, scope=scope, user_id=user_id)


@mcp.tool(description="Return the OneDrive connector runtime context, including configured user ID.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
