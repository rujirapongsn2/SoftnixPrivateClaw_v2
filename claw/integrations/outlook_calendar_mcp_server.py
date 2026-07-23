"""Outlook Calendar MCP server for the built-in Outlook Calendar connector preset."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT = "https://graph.microsoft.com/v1.0"
OUTLOOK_CALENDAR_USER_ID_DEFAULT = "me"
OUTLOOK_CALENDAR_TENANT_ID_DEFAULT = "organizations"
OUTLOOK_CALENDAR_USER_AGENT = "nanobot-outlook-calendar-connector/1.0"
OUTLOOK_CALENDAR_WRITE_SCOPES = {"Calendars.ReadWrite"}
OUTLOOK_CALENDAR_READ_SCOPES = {"Calendars.Read", "Calendars.ReadWrite"}


@dataclass
class OutlookCalendarClient:
    """Small Microsoft Graph calendar client used by the MCP server and validation flow."""

    token: str
    graph_base: str = OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT
    user_id: str = OUTLOOK_CALENDAR_USER_ID_DEFAULT
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = OUTLOOK_CALENDAR_TENANT_ID_DEFAULT
    token_uri: str = ""
    scopes: str = ""
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": OUTLOOK_CALENDAR_USER_AGENT,
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
        retry_on_auth_error: bool = True,
    ) -> Any:
        if not self.token:
            raise ValueError("Outlook Calendar access token is required")
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

    def _request_url(self, url: str) -> dict[str, Any]:
        if not self.token:
            raise ValueError("Outlook Calendar access token is required")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": OUTLOOK_CALENDAR_USER_AGENT,
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
        resolved = str(user_id or self.user_id or OUTLOOK_CALENDAR_USER_ID_DEFAULT).strip()
        return resolved or OUTLOOK_CALENDAR_USER_ID_DEFAULT

    def _user_path(self, user_id: str | None = None) -> str:
        resolved = self._resolve_user_id(user_id)
        if resolved.lower() == "me":
            return "/me"
        return f"/users/{resolved}"

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", self._user_path())

    def list_calendars(self, *, top: int = 25, user_id: str | None = None) -> dict[str, Any]:
        params = {"$top": int(top)}
        return self._request("GET", f"{self._user_path(user_id)}/calendars", params=params)

    def list_events(
        self,
        *,
        calendar_id: str | None = None,
        top: int = 25,
        select: str | None = None,
        filter: str | None = None,
        search: str | None = None,
        order_by: str | None = None,
        start_date_time: str | None = None,
        end_date_time: str | None = None,
        page_url: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if page_url:
            return self._request_url(page_url)
        params: dict[str, Any] = {"$top": int(top)}
        if select:
            params["$select"] = str(select).strip()
        else:
            params["$select"] = (
                "id,subject,start,end,location,organizer,attendees,isOnlineMeeting,onlineMeetingProvider"
            )
        if filter:
            params["$filter"] = str(filter).strip()
        if search:
            params["$search"] = str(search).strip()
        if order_by:
            params["$orderby"] = str(order_by).strip()
        if start_date_time and end_date_time:
            params["startDateTime"] = str(start_date_time).strip()
            params["endDateTime"] = str(end_date_time).strip()
            if calendar_id:
                path = f"{self._user_path(user_id)}/calendars/{str(calendar_id).strip()}/calendarView"
            else:
                path = f"{self._user_path(user_id)}/calendarView"
        else:
            if calendar_id:
                path = f"{self._user_path(user_id)}/calendars/{str(calendar_id).strip()}/events"
            else:
                path = f"{self._user_path(user_id)}/events"
        return self._request("GET", path, params=params)

    def get_event(
        self,
        event_id: str,
        *,
        select: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"$select": str(select).strip()} if str(select or "").strip() else None
        return self._request(
            "GET", f"{self._user_path(user_id)}/events/{str(event_id).strip()}", params=params
        )

    def create_event(
        self,
        *,
        subject: str,
        start_date_time: str,
        end_date_time: str,
        timezone: str = "UTC",
        body: str = "",
        body_html: str | None = None,
        location: str | None = None,
        attendees: str | None = None,
        is_online_meeting: bool = False,
        online_meeting_provider: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Calendars.ReadWrite")
        payload = _build_event_payload(
            subject=subject,
            start_date_time=start_date_time,
            end_date_time=end_date_time,
            timezone=timezone,
            body=body,
            body_html=body_html,
            location=location,
            attendees=attendees,
            is_online_meeting=is_online_meeting,
            online_meeting_provider=online_meeting_provider,
        )
        return self._request("POST", f"{self._user_path(user_id)}/events", json_data=payload)

    def update_event(
        self,
        event_id: str,
        *,
        subject: str | None = None,
        start_date_time: str | None = None,
        end_date_time: str | None = None,
        timezone: str | None = None,
        body: str | None = None,
        body_html: str | None = None,
        location: str | None = None,
        attendees: str | None = None,
        is_online_meeting: bool | None = None,
        online_meeting_provider: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Calendars.ReadWrite")
        payload: dict[str, Any] = {}
        if subject is not None:
            payload["subject"] = str(subject)
        if body is not None or body_html is not None:
            payload["body"] = {
                "contentType": "HTML" if body_html is not None else "Text",
                "content": str(body_html if body_html is not None else body or ""),
            }
        if start_date_time is not None:
            payload["start"] = {
                "dateTime": str(start_date_time),
                "timeZone": str(timezone or "UTC"),
            }
        if end_date_time is not None:
            payload["end"] = {
                "dateTime": str(end_date_time),
                "timeZone": str(timezone or "UTC"),
            }
        if location is not None:
            payload["location"] = {"displayName": str(location)}
        if attendees is not None:
            payload["attendees"] = _build_attendees(attendees)
        if is_online_meeting is not None:
            payload["isOnlineMeeting"] = bool(is_online_meeting)
        if online_meeting_provider is not None:
            payload["onlineMeetingProvider"] = str(online_meeting_provider)
        if not payload:
            raise ValueError("At least one event field is required for update")
        return self._request(
            "PATCH", f"{self._user_path(user_id)}/events/{str(event_id).strip()}", json_data=payload
        )

    def delete_event(self, event_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Calendars.ReadWrite")
        return self._request("DELETE", f"{self._user_path(user_id)}/events/{str(event_id).strip()}")

    def get_schedule(
        self,
        *,
        schedules: str,
        start_date_time: str,
        end_date_time: str,
        timezone: str = "UTC",
        availability_view_interval: int = 30,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Free/busy lookup via Graph's getSchedule — works for any mailbox in the
        tenant under plain Calendars.Read(Write), unlike /events which 403s
        without the target explicitly sharing their calendar."""
        schedule_list = _normalize_recipients(schedules)
        if not schedule_list:
            raise ValueError("At least one schedule (email address) is required")
        payload = {
            "schedules": schedule_list,
            "startTime": {"dateTime": str(start_date_time), "timeZone": str(timezone or "UTC")},
            "endTime": {"dateTime": str(end_date_time), "timeZone": str(timezone or "UTC")},
            "availabilityViewInterval": int(availability_view_interval),
        }
        return self._request("POST", f"{self._user_path(user_id)}/calendar/getSchedule", json_data=payload)

    def token_scopes(self) -> set[str]:
        return _decode_access_token_permissions(self.token)

    def ensure_write_scope(self, *, required_scope: str | None = None) -> set[str]:
        scopes = self.token_scopes()
        if required_scope and required_scope in scopes:
            return scopes
        if not required_scope and scopes.intersection(OUTLOOK_CALENDAR_WRITE_SCOPES):
            return scopes
        raise ValueError(
            "Outlook Calendar token does not include the required Microsoft Graph calendar write scope. "
            "Regenerate the token with Calendars.ReadWrite for event updates."
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
            headers={"Accept": "application/json", "User-Agent": OUTLOOK_CALENDAR_USER_AGENT},
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
            raise ValueError("Outlook Calendar token refresh did not return an access token")
        self.token = new_token
        returned_refresh = str(payload.get("refresh_token") or "").strip()
        if returned_refresh:
            self.refresh_token = returned_refresh
        return True

    def _resolved_token_uri(self) -> str:
        explicit = str(self.token_uri or "").strip()
        if explicit:
            return explicit
        tenant = (
            str(self.tenant_id or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT).strip()
            or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT
        )
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def has_refresh_credentials(self) -> bool:
        return bool(str(self.refresh_token or "").strip() and str(self.client_id or "").strip())


def _normalize_recipients(value: str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = [str(value)]
    return [item.strip() for item in items if item and item.strip()]


def _build_attendees(value: str | None) -> list[dict[str, Any]]:
    return [
        {"emailAddress": {"address": address}, "type": "required"} for address in _normalize_recipients(value)
    ]


def _build_event_payload(
    *,
    subject: str,
    start_date_time: str,
    end_date_time: str,
    timezone: str,
    body: str = "",
    body_html: str | None = None,
    location: str | None = None,
    attendees: str | None = None,
    is_online_meeting: bool = False,
    online_meeting_provider: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject": str(subject or "").strip(),
        "body": {
            "contentType": "HTML" if body_html is not None else "Text",
            "content": str(body_html if body_html is not None else body or ""),
        },
        "start": {"dateTime": str(start_date_time), "timeZone": str(timezone or "UTC")},
        "end": {"dateTime": str(end_date_time), "timeZone": str(timezone or "UTC")},
    }
    if location:
        payload["location"] = {"displayName": str(location).strip()}
    attendees_payload = _build_attendees(attendees)
    if attendees_payload:
        payload["attendees"] = attendees_payload
    if is_online_meeting:
        payload["isOnlineMeeting"] = True
        if online_meeting_provider:
            payload["onlineMeetingProvider"] = str(online_meeting_provider)
    return payload


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


def _client_from_env() -> OutlookCalendarClient:
    tenant_id = (
        str(os.environ.get("OUTLOOK_CALENDAR_TENANT_ID") or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT).strip()
        or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT
    )
    return OutlookCalendarClient(
        token=str(os.environ.get("OUTLOOK_CALENDAR_TOKEN") or "").strip(),
        graph_base=str(
            os.environ.get("OUTLOOK_CALENDAR_GRAPH_BASE") or OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT
        ).strip()
        or OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT,
        user_id=str(os.environ.get("OUTLOOK_CALENDAR_USER_ID") or OUTLOOK_CALENDAR_USER_ID_DEFAULT).strip()
        or OUTLOOK_CALENDAR_USER_ID_DEFAULT,
        refresh_token=str(os.environ.get("OUTLOOK_CALENDAR_REFRESH_TOKEN") or "").strip(),
        client_id=str(os.environ.get("OUTLOOK_CALENDAR_CLIENT_ID") or "").strip(),
        client_secret=str(os.environ.get("OUTLOOK_CALENDAR_CLIENT_SECRET") or "").strip(),
        tenant_id=tenant_id,
        token_uri=str(os.environ.get("OUTLOOK_CALENDAR_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        scopes=str(os.environ.get("OUTLOOK_CALENDAR_SCOPES") or "").strip(),
    )


def _connector_context() -> dict[str, Any]:
    tenant_id = (
        str(os.environ.get("OUTLOOK_CALENDAR_TENANT_ID") or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT).strip()
        or OUTLOOK_CALENDAR_TENANT_ID_DEFAULT
    )
    default_user_id = (
        str(os.environ.get("OUTLOOK_CALENDAR_USER_ID") or OUTLOOK_CALENDAR_USER_ID_DEFAULT).strip()
        or OUTLOOK_CALENDAR_USER_ID_DEFAULT
    )
    return {
        "graph_base": str(
            os.environ.get("OUTLOOK_CALENDAR_GRAPH_BASE") or OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT
        ).strip()
        or OUTLOOK_CALENDAR_GRAPH_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("OUTLOOK_CALENDAR_TOKEN") or "").strip()),
        "has_refresh_token": bool(str(os.environ.get("OUTLOOK_CALENDAR_REFRESH_TOKEN") or "").strip()),
        "has_client_id": bool(str(os.environ.get("OUTLOOK_CALENDAR_CLIENT_ID") or "").strip()),
        "default_user_id": default_user_id,
        "effective_user_id": default_user_id,
        "tenant_id": tenant_id,
        "token_uri": str(os.environ.get("OUTLOOK_CALENDAR_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        "capabilities": ["calendar.read", "calendar.write", "calendar.freebusy"],
    }


mcp = FastMCP(
    "outlook-calendar-connector",
    instructions=(
        "Outlook Calendar connector for calendar list, event search, event inspection, "
        "event create/update/delete, and free/busy schedule lookups (including other "
        "mailboxes in the same tenant) through Microsoft Graph."
    ),
)


@mcp.tool(description="Return the authenticated Microsoft Graph user profile for token validation.")
def whoami() -> dict[str, Any]:
    return _client_from_env().whoami()


@mcp.tool(description="List Outlook calendars for the configured mailbox.")
def list_calendars(top: int = 25, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().list_calendars(top=top, user_id=user_id)


@mcp.tool(description="List Outlook calendar events with optional filtering, calendarView, or paging URL.")
def list_events(
    calendar_id: str | None = None,
    top: int = 25,
    select: str | None = None,
    filter: str | None = None,
    search: str | None = None,
    order_by: str | None = None,
    start_date_time: str | None = None,
    end_date_time: str | None = None,
    page_url: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_events(
        calendar_id=calendar_id,
        top=top,
        select=select,
        filter=filter,
        search=search,
        order_by=order_by,
        start_date_time=start_date_time,
        end_date_time=end_date_time,
        page_url=page_url,
        user_id=user_id,
    )


@mcp.tool(description="Get one Outlook calendar event by ID.")
def get_event(event_id: str, select: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_event(event_id=event_id, select=select, user_id=user_id)


@mcp.tool(description="Create an Outlook calendar event.")
def create_event(
    subject: str,
    start_date_time: str,
    end_date_time: str,
    timezone: str = "UTC",
    body: str = "",
    body_html: str | None = None,
    location: str | None = None,
    attendees: str | None = None,
    is_online_meeting: bool = False,
    online_meeting_provider: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_event(
        subject=subject,
        start_date_time=start_date_time,
        end_date_time=end_date_time,
        timezone=timezone,
        body=body,
        body_html=body_html,
        location=location,
        attendees=attendees,
        is_online_meeting=is_online_meeting,
        online_meeting_provider=online_meeting_provider,
        user_id=user_id,
    )


@mcp.tool(description="Update an Outlook calendar event by ID.")
def update_event(
    event_id: str,
    subject: str | None = None,
    start_date_time: str | None = None,
    end_date_time: str | None = None,
    timezone: str | None = None,
    body: str | None = None,
    body_html: str | None = None,
    location: str | None = None,
    attendees: str | None = None,
    is_online_meeting: bool | None = None,
    online_meeting_provider: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().update_event(
        event_id=event_id,
        subject=subject,
        start_date_time=start_date_time,
        end_date_time=end_date_time,
        timezone=timezone,
        body=body,
        body_html=body_html,
        location=location,
        attendees=attendees,
        is_online_meeting=is_online_meeting,
        online_meeting_provider=online_meeting_provider,
        user_id=user_id,
    )


@mcp.tool(description="Delete an Outlook calendar event by ID.")
def delete_event(event_id: str, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().delete_event(event_id=event_id, user_id=user_id)


@mcp.tool(
    description=(
        "Check free/busy availability for one or more mailboxes (e.g. colleagues in the same "
        "tenant) over a time range, without needing them to explicitly share their calendar. "
        "schedules is a comma/semicolon-separated list of email addresses/UPNs."
    )
)
def get_schedule(
    schedules: str,
    start_date_time: str,
    end_date_time: str,
    timezone: str = "UTC",
    availability_view_interval: int = 30,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().get_schedule(
        schedules=schedules,
        start_date_time=start_date_time,
        end_date_time=end_date_time,
        timezone=timezone,
        availability_view_interval=availability_view_interval,
        user_id=user_id,
    )


@mcp.tool(
    description="Return the Outlook Calendar connector runtime context, including configured mailbox user ID."
)
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
