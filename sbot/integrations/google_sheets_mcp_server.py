"""Google Sheets MCP server for the built-in Google Sheets connector preset."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

SHEETS_API_BASE_DEFAULT = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_USER_AGENT = "nanobot-google-sheets-connector/1.0"
SHEETS_WRITE_SCOPES = {"https://www.googleapis.com/auth/spreadsheets"}

# Accept a full Google Sheets URL (any of its /u/<n>/, query-param, or
# #gid=... variants) wherever a spreadsheet_id is expected — done server-side
# rather than relying on the model to isolate the id from a pasted link
# before calling a tool, so a user can paste the sheet's URL as-is and it
# always resolves the same way regardless of model behavior.
_SPREADSHEET_URL_ID_RE = re.compile(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)")
_PUBLISHED_LINK_RE = re.compile(r"/spreadsheets/(?:u/\d+/)?d/e/")
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _extract_spreadsheet_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("spreadsheet_id is required")
    if _PUBLISHED_LINK_RE.search(raw):
        raise ValueError(
            "This looks like a Google Sheets 'Publish to the web' link, which does not expose a "
            "usable spreadsheet ID. Open the sheet normally instead and pass its address-bar URL "
            "(…/spreadsheets/d/<ID>/edit) or share link."
        )
    match = _SPREADSHEET_URL_ID_RE.search(raw)
    spreadsheet_id = match.group(1) if match else raw
    if not _VALID_ID_RE.match(spreadsheet_id):
        raise ValueError(
            f"Could not find a spreadsheet ID in {raw!r}. Pass the raw spreadsheet ID or a full "
            "Google Sheets URL (e.g. .../spreadsheets/d/<ID>/edit)."
        )
    return spreadsheet_id


@dataclass
class GoogleSheetsClient:
    """Small Google Sheets REST API (v4) client used by the MCP server."""

    token: str
    api_base: str = SHEETS_API_BASE_DEFAULT
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    token_uri: str = "https://oauth2.googleapis.com/token"
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": SHEETS_USER_AGENT,
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
        json_data: dict[str, Any] | None = None,
        retry_on_auth_error: bool = True,
    ) -> Any:
        if not self.token:
            raise ValueError("Google Sheets access token is required")
        with self._client() as client:
            response = client.request(method, path, params=params, json=json_data)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if retry_on_auth_error and response.status_code == 401 and self._refresh_access_token():
                    return self._request(
                        method,
                        path,
                        params=params,
                        json_data=json_data,
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
            return response.json()

    def _refresh_access_token(self) -> bool:
        refresh_token = str(self.refresh_token or "").strip()
        client_id = str(self.client_id or "").strip()
        client_secret = str(self.client_secret or "").strip()
        token_uri = str(self.token_uri or "").strip() or "https://oauth2.googleapis.com/token"
        if not (refresh_token and client_id):
            return False
        form_data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            form_data["client_secret"] = client_secret
        with httpx.Client(
            timeout=20.0,
            transport=self.transport,
            headers={"Accept": "application/json", "User-Agent": SHEETS_USER_AGENT},
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
            raise ValueError("Google Sheets token refresh did not return an access token")
        self.token = new_token
        returned_refresh = str(payload.get("refresh_token") or "").strip()
        if returned_refresh:
            self.refresh_token = returned_refresh
        return True

    def token_scopes(self) -> set[str]:
        data = self._tokeninfo()
        scopes = str(data.get("scope") or "").split()
        return {scope.strip() for scope in scopes if scope.strip()}

    def _tokeninfo(self) -> dict[str, Any]:
        with httpx.Client(
            timeout=20.0,
            transport=self.transport,
            headers={"Accept": "application/json", "User-Agent": SHEETS_USER_AGENT},
        ) as client:
            response = client.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": self.token})
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code == 401 and self._refresh_access_token():
                    return self._tokeninfo()
                detail = _extract_response_detail(response)
                if detail:
                    raise httpx.HTTPStatusError(
                        f"{exc}: {detail}",
                        request=exc.request,
                        response=exc.response,
                    ) from exc
                raise
            return response.json()

    def ensure_write_scope(self) -> set[str]:
        scopes = self.token_scopes()
        if scopes.intersection(SHEETS_WRITE_SCOPES):
            return scopes
        raise ValueError(
            "Google Sheets token does not include the spreadsheets write scope. Reconnect the "
            "connector to grant write access."
        )

    def get_spreadsheet(self, spreadsheet_id: str, *, include_grid_data: bool = False) -> dict[str, Any]:
        params = {"includeGridData": "true"} if include_grid_data else None
        return self._request("GET", f"/{_extract_spreadsheet_id(spreadsheet_id)}", params=params)

    def list_sheets(self, spreadsheet_id: str) -> dict[str, Any]:
        data = self.get_spreadsheet(spreadsheet_id)
        sheets: list[dict[str, Any]] = []
        for sheet in data.get("sheets") or []:
            props = sheet.get("properties") or {}
            grid = props.get("gridProperties") or {}
            sheets.append(
                {
                    "sheetId": props.get("sheetId"),
                    "title": props.get("title"),
                    "index": props.get("index"),
                    "rowCount": grid.get("rowCount"),
                    "columnCount": grid.get("columnCount"),
                }
            )
        return {
            "spreadsheetId": data.get("spreadsheetId"),
            "title": (data.get("properties") or {}).get("title"),
            "sheets": sheets,
        }

    def read_range(
        self, spreadsheet_id: str, range: str, *, value_render_option: str = "FORMATTED_VALUE"
    ) -> dict[str, Any]:
        range_clean = _clean(range)
        return self._request(
            "GET",
            f"/{_extract_spreadsheet_id(spreadsheet_id)}/values/{quote(range_clean, safe='!:$')}",
            params={"valueRenderOption": value_render_option},
        )

    def update_range(
        self,
        spreadsheet_id: str,
        range: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        self.ensure_write_scope()
        range_clean = _clean(range)
        return self._request(
            "PUT",
            f"/{_extract_spreadsheet_id(spreadsheet_id)}/values/{quote(range_clean, safe='!:$')}",
            params={"valueInputOption": value_input_option},
            json_data={"range": range_clean, "values": values},
        )

    def append_row(
        self,
        spreadsheet_id: str,
        range: str,
        values: list[list[Any]],
        *,
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        self.ensure_write_scope()
        range_clean = _clean(range)
        return self._request(
            "POST",
            f"/{_extract_spreadsheet_id(spreadsheet_id)}/values/{quote(range_clean, safe='!:$')}:append",
            params={"valueInputOption": value_input_option, "insertDataOption": "INSERT_ROWS"},
            json_data={"range": range_clean, "values": values},
        )

    def clear_range(self, spreadsheet_id: str, range: str) -> dict[str, Any]:
        self.ensure_write_scope()
        range_clean = _clean(range)
        return self._request(
            "POST", f"/{_extract_spreadsheet_id(spreadsheet_id)}/values/{quote(range_clean, safe='!:$')}:clear"
        )

    def create_spreadsheet(self, title: str, sheet_titles: list[str] | None = None) -> dict[str, Any]:
        self.ensure_write_scope()
        body: dict[str, Any] = {
            "properties": {"title": str(title or "").strip() or "Untitled spreadsheet"}
        }
        titles = [str(t).strip() for t in (sheet_titles or []) if str(t or "").strip()]
        if titles:
            body["sheets"] = [{"properties": {"title": t}} for t in titles]
        # POST to the bare collection endpoint (no id yet) — pass the absolute
        # URL rather than a relative path, since a relative "" joins onto
        # self.api_base with a trailing slash that some routers treat as a
        # different route than the exact base URL.
        return self._request("POST", self.api_base, json_data=body)


def _clean(value: str) -> str:
    return str(value or "").strip()


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
            errors = error.get("errors")
            if isinstance(errors, list):
                for item in errors:
                    if isinstance(item, dict):
                        reason = str(item.get("reason") or "").strip()
                        location = str(item.get("location") or "").strip()
                        if reason and location:
                            messages.append(f"{reason} at {location}")
                        elif reason:
                            messages.append(reason)
            return "; ".join(messages)
    return ""


def _client_from_env() -> GoogleSheetsClient:
    return GoogleSheetsClient(
        token=str(os.environ.get("GOOGLE_SHEETS_TOKEN") or "").strip(),
        api_base=str(os.environ.get("GOOGLE_SHEETS_API_BASE") or SHEETS_API_BASE_DEFAULT).strip()
        or SHEETS_API_BASE_DEFAULT,
        refresh_token=str(os.environ.get("GOOGLE_SHEETS_REFRESH_TOKEN") or "").strip(),
        client_id=str(os.environ.get("GOOGLE_SHEETS_CLIENT_ID") or "").strip(),
        client_secret=str(os.environ.get("GOOGLE_SHEETS_CLIENT_SECRET") or "").strip(),
        token_uri=str(os.environ.get("GOOGLE_SHEETS_TOKEN_URI") or "https://oauth2.googleapis.com/token").strip()
        or "https://oauth2.googleapis.com/token",
    )


def _connector_context() -> dict[str, Any]:
    return {
        "api_base": str(os.environ.get("GOOGLE_SHEETS_API_BASE") or SHEETS_API_BASE_DEFAULT).strip()
        or SHEETS_API_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("GOOGLE_SHEETS_TOKEN") or "").strip()),
        "has_refresh_token": bool(str(os.environ.get("GOOGLE_SHEETS_REFRESH_TOKEN") or "").strip()),
        "has_client_id": bool(str(os.environ.get("GOOGLE_SHEETS_CLIENT_ID") or "").strip()),
        "token_uri": str(os.environ.get("GOOGLE_SHEETS_TOKEN_URI") or "https://oauth2.googleapis.com/token").strip()
        or "https://oauth2.googleapis.com/token",
        "capabilities": ["read", "write", "create"],
    }


mcp = FastMCP(
    "google-sheets-connector",
    instructions=(
        "Google Sheets connector for reading and updating spreadsheet data. Every tool's "
        "spreadsheet_id parameter accepts either the raw spreadsheet ID or the full Google Sheets "
        "URL (e.g. one pasted straight from the browser, with any ?gid=...#gid=... suffix) — pass "
        "whichever the user gave you as-is, no need to extract the ID yourself. Ranges use A1 "
        "notation, e.g. 'Sheet1!A1:C10'. Call list_sheets first if the sheet/tab name isn't already "
        "known."
    ),
)


@mcp.tool(
    description=(
        "Get a spreadsheet's title and its sheet tabs (name, id, row/column counts). spreadsheet_id "
        "accepts either the raw ID or the full Sheets URL."
    )
)
def list_sheets(spreadsheet_id: str) -> dict[str, Any]:
    return _client_from_env().list_sheets(spreadsheet_id)


@mcp.tool(
    description=(
        "Read cell values from a range (A1 notation, e.g. 'Sheet1!A1:C10') in a spreadsheet. "
        "spreadsheet_id accepts either the raw ID or the full Sheets URL."
    )
)
def read_range(spreadsheet_id: str, range: str, value_render_option: str = "FORMATTED_VALUE") -> dict[str, Any]:
    return _client_from_env().read_range(spreadsheet_id, range, value_render_option=value_render_option)


@mcp.tool(
    description=(
        "Overwrite cell values in a range (A1 notation) in a spreadsheet. `values` is a list "
        "of rows, each row a list of cell values. spreadsheet_id accepts either the raw ID or the "
        "full Sheets URL."
    )
)
def update_range(
    spreadsheet_id: str, range: str, values: list[list[Any]], value_input_option: str = "USER_ENTERED"
) -> dict[str, Any]:
    return _client_from_env().update_range(spreadsheet_id, range, values, value_input_option=value_input_option)


@mcp.tool(
    description=(
        "Append one or more rows after the last row of data in a range/sheet (A1 notation) in a "
        "spreadsheet. `values` is a list of rows, each row a list of cell values. spreadsheet_id "
        "accepts either the raw ID or the full Sheets URL."
    )
)
def append_row(
    spreadsheet_id: str, range: str, values: list[list[Any]], value_input_option: str = "USER_ENTERED"
) -> dict[str, Any]:
    return _client_from_env().append_row(spreadsheet_id, range, values, value_input_option=value_input_option)


@mcp.tool(
    description=(
        "Clear all values (not formatting) from a range (A1 notation) in a spreadsheet. "
        "spreadsheet_id accepts either the raw ID or the full Sheets URL."
    )
)
def clear_range(spreadsheet_id: str, range: str) -> dict[str, Any]:
    return _client_from_env().clear_range(spreadsheet_id, range)


@mcp.tool(
    description=(
        "Create a new Google Sheets spreadsheet with the given title, optionally with named sheet "
        "tabs. Returns the new spreadsheetId and a shareable URL."
    )
)
def create_spreadsheet(title: str, sheet_titles: list[str] | None = None) -> dict[str, Any]:
    result = _client_from_env().create_spreadsheet(title, sheet_titles=sheet_titles)
    spreadsheet_id = str(result.get("spreadsheetId") or "").strip()
    if spreadsheet_id and not result.get("spreadsheetUrl"):
        result["spreadsheetUrl"] = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return result


@mcp.tool(description="Return the Google Sheets connector runtime context (token/refresh status, capabilities).")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
