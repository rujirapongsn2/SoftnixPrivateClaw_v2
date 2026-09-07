"""Outlook Microsoft 365 MCP server for the built-in Outlook connector preset."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

OUTLOOK_GRAPH_BASE_DEFAULT = "https://graph.microsoft.com/v1.0"
OUTLOOK_USER_ID_DEFAULT = "me"
OUTLOOK_TENANT_ID_DEFAULT = "organizations"
OUTLOOK_USER_AGENT = "nanobot-outlook-connector/1.0"
OUTLOOK_WRITE_SCOPES = {"Mail.Send", "Mail.ReadWrite"}
OUTLOOK_SUMMARY_SELECT = "id,subject,from,receivedDateTime,hasAttachments,isRead,bodyPreview,webLink"
OUTLOOK_MESSAGE_TEXT_SELECT = "id,subject,from,receivedDateTime,hasAttachments,isRead,body,bodyPreview,webLink"
OUTLOOK_MESSAGE_TEXT_RECIPIENT_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,isRead,body,bodyPreview,webLink"


@dataclass
class OutlookClient:
    """Small Microsoft Graph mail client used by the MCP server and validation flow."""

    token: str
    graph_base: str = OUTLOOK_GRAPH_BASE_DEFAULT
    user_id: str = OUTLOOK_USER_ID_DEFAULT
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    tenant_id: str = OUTLOOK_TENANT_ID_DEFAULT
    token_uri: str = ""
    scopes: str = ""
    transport: httpx.BaseTransport | None = None

    def _client(self, *, prefer_body_type: str | None = None) -> httpx.Client:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": OUTLOOK_USER_AGENT,
        }
        if prefer_body_type:
            headers["Prefer"] = f'outlook.body-content-type="{prefer_body_type}"'
        return httpx.Client(
            base_url=self.graph_base.rstrip("/"),
            headers=headers,
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
        prefer_body_type: str | None = None,
        retry_on_auth_error: bool = True,
    ) -> Any:
        if not self.token:
            raise ValueError("Outlook access token is required")
        with self._client(prefer_body_type=prefer_body_type) as client:
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
                        prefer_body_type=prefer_body_type,
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

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        resolved = str(user_id or self.user_id or OUTLOOK_USER_ID_DEFAULT).strip()
        return resolved or OUTLOOK_USER_ID_DEFAULT

    def _user_path(self, user_id: str | None = None) -> str:
        resolved = self._resolve_user_id(user_id)
        if resolved.lower() == "me":
            return "/me"
        return f"/users/{resolved}"

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", self._user_path())

    def list_mail_folders(
        self,
        *,
        top: int = 25,
        include_hidden: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"$top": int(top), "includeHiddenFolders": bool(include_hidden)}
        return self._request("GET", f"{self._user_path(user_id)}/mailFolders", params=params)

    def list_messages(
        self,
        *,
        folder_id: str | None = None,
        top: int = 10,
        select: str | None = None,
        filter: str | None = None,
        search: str | None = None,
        order_by: str | None = None,
        page_url: str | None = None,
        body_content_type: str | None = "text",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if page_url:
            return self._request_url(page_url, prefer_body_type=body_content_type)
        params: dict[str, Any] = {"$top": int(top)}
        if select:
            params["$select"] = str(select).strip()
        else:
            params["$select"] = "id,subject,from,toRecipients,receivedDateTime,hasAttachments,isRead,bodyPreview"
        if filter:
            params["$filter"] = str(filter).strip()
        if search:
            params["$search"] = str(search).strip()
        if order_by:
            params["$orderby"] = str(order_by).strip()
        folder = str(folder_id or "").strip()
        path = f"{self._user_path(user_id)}/mailFolders/{folder}/messages" if folder else f"{self._user_path(user_id)}/messages"
        return self._request("GET", path, params=params, prefer_body_type=body_content_type)

    def list_message_summaries(
        self,
        *,
        folder_id: str | None = None,
        top: int = 20,
        filter: str | None = None,
        search: str | None = None,
        order_by: str | None = "receivedDateTime desc",
        page_url: str | None = None,
        preview_chars: int = 500,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        used_top = _bounded_int(top, default=20, minimum=1, maximum=50)
        used_preview_chars = _bounded_int(preview_chars, default=500, minimum=80, maximum=2000)
        if page_url:
            response = self._request_url(page_url, prefer_body_type="text")
        else:
            response = self.list_messages(
                folder_id=folder_id,
                top=used_top,
                select=OUTLOOK_SUMMARY_SELECT,
                filter=filter,
                search=search,
                order_by=order_by,
                body_content_type="text",
                user_id=user_id,
            )
        messages = response.get("value") if isinstance(response, dict) else []
        if not isinstance(messages, list):
            messages = []
        compact = [_compact_message_summary(message, preview_chars=used_preview_chars) for message in messages[:used_top]]
        result: dict[str, Any] = {
            "value": compact,
            "count": len(compact),
            "top_used": used_top,
            "preview_chars": used_preview_chars,
            "truncated_previews": sum(1 for item in compact if item.get("bodyPreview_truncated")),
        }
        if isinstance(response, dict) and response.get("@odata.nextLink"):
            result["@odata.nextLink"] = response.get("@odata.nextLink")
        return result

    def _request_url(self, url: str, *, prefer_body_type: str | None = "text") -> dict[str, Any]:
        if not self.token:
            raise ValueError("Outlook access token is required")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": OUTLOOK_USER_AGENT,
        }
        if prefer_body_type:
            headers["Prefer"] = f'outlook.body-content-type="{prefer_body_type}"'
        with httpx.Client(timeout=20.0, transport=self.transport, headers=headers) as client:
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

    def get_message(
        self,
        message_id: str,
        *,
        select: str | None = None,
        body_content_type: str | None = "text",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        params = {"$select": str(select).strip()} if str(select or "").strip() else None
        return self._request(
            "GET",
            f"{self._user_path(user_id)}/messages/{str(message_id).strip()}",
            params=params,
            prefer_body_type=body_content_type,
        )

    def get_message_text(
        self,
        message_id: str,
        *,
        max_body_chars: int = 12_000,
        include_recipients: bool = False,
        strip_quoted_replies: bool = True,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        used_max_body_chars = _bounded_int(max_body_chars, default=12_000, minimum=500, maximum=50_000)
        message = self.get_message(
            message_id,
            select=OUTLOOK_MESSAGE_TEXT_RECIPIENT_SELECT if include_recipients else OUTLOOK_MESSAGE_TEXT_SELECT,
            body_content_type="text",
            user_id=user_id,
        )
        body = message.get("body") if isinstance(message.get("body"), dict) else {}
        body_text = str(body.get("content") or message.get("bodyPreview") or "")
        if strip_quoted_replies:
            body_text = _strip_quoted_reply(body_text)
        body_text, truncated = _truncate_text(body_text, used_max_body_chars)
        result = _compact_message_summary(message, preview_chars=500)
        result.update(
            {
                "body_text": body_text,
                "body_chars": len(body_text),
                "body_truncated": truncated,
                "max_body_chars": used_max_body_chars,
            }
        )
        if include_recipients:
            result["toRecipients"] = _compact_recipients(message.get("toRecipients"))
            result["ccRecipients"] = _compact_recipients(message.get("ccRecipients"))
        return result

    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        body_html: str | None = None,
        reply_to: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Mail.ReadWrite")
        return self._request(
            "POST",
            f"{self._user_path(user_id)}/messages",
            json_data=self._build_message_payload(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                body_html=body_html,
                reply_to=reply_to,
            ),
        )

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        body_html: str | None = None,
        reply_to: str | None = None,
        save_to_sent_items: bool = True,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Mail.Send")
        payload = {
            "message": self._build_message_payload(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                bcc=bcc,
                body_html=body_html,
                reply_to=reply_to,
            ),
            "saveToSentItems": bool(save_to_sent_items),
        }
        return self._request("POST", f"{self._user_path(user_id)}/sendMail", json_data=payload)

    def send_draft(
        self,
        message_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_write_scope(required_scope="Mail.Send")
        return self._request("POST", f"{self._user_path(user_id)}/messages/{str(message_id).strip()}/send")

    def _build_message_payload(
        self,
        *,
        to: str,
        subject: str,
        body: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        body_html: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        to_list = _normalize_recipients(to)
        cc_list = _normalize_recipients(cc)
        bcc_list = _normalize_recipients(bcc)
        reply_to_list = _normalize_recipients(reply_to)
        if not to_list:
            raise ValueError("Outlook message recipient is required")
        payload: dict[str, Any] = {
            "subject": str(subject or "").strip(),
            "body": {
                "contentType": "HTML" if body_html else "Text",
                "content": str(body_html if body_html is not None else body or ""),
            },
            "toRecipients": _build_graph_recipients(to_list),
        }
        if cc_list:
            payload["ccRecipients"] = _build_graph_recipients(cc_list)
        if bcc_list:
            payload["bccRecipients"] = _build_graph_recipients(bcc_list)
        if reply_to_list:
            payload["replyTo"] = _build_graph_recipients(reply_to_list)
        return payload

    def token_scopes(self) -> set[str]:
        return _decode_access_token_permissions(self.token)

    def ensure_write_scope(self, *, required_scope: str | None = None) -> set[str]:
        scopes = self.token_scopes()
        if required_scope and required_scope in scopes:
            return scopes
        if not required_scope and scopes.intersection(OUTLOOK_WRITE_SCOPES):
            return scopes
        raise ValueError(
            "Outlook token does not include the required Microsoft Graph mail write scope. "
            "Regenerate the token with Mail.ReadWrite for draft support or Mail.Send for send support."
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
            headers={"Accept": "application/json", "User-Agent": OUTLOOK_USER_AGENT},
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
            raise ValueError("Outlook token refresh did not return an access token")
        self.token = new_token
        returned_refresh = str(payload.get("refresh_token") or "").strip()
        if returned_refresh:
            self.refresh_token = returned_refresh
        return True

    def _resolved_token_uri(self) -> str:
        explicit = str(self.token_uri or "").strip()
        if explicit:
            return explicit
        tenant = str(self.tenant_id or OUTLOOK_TENANT_ID_DEFAULT).strip() or OUTLOOK_TENANT_ID_DEFAULT
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


def _build_graph_recipients(addresses: list[str]) -> list[dict[str, dict[str, str]]]:
    return [{"emailAddress": {"address": item}} for item in addresses]


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = int(default)
    return max(int(minimum), min(int(maximum), number))


def _truncate_text(value: Any, max_chars: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= int(max_chars):
        return text, False
    return text[: int(max_chars)].rstrip() + "…", True


def _compact_message_summary(message: Any, *, preview_chars: int) -> dict[str, Any]:
    item = message if isinstance(message, dict) else {}
    preview, preview_truncated = _truncate_text(item.get("bodyPreview"), int(preview_chars))
    return {
        "id": item.get("id"),
        "subject": item.get("subject"),
        "from": _compact_email_address(item.get("from")),
        "receivedDateTime": item.get("receivedDateTime"),
        "isRead": item.get("isRead"),
        "hasAttachments": item.get("hasAttachments"),
        "bodyPreview": preview,
        "bodyPreview_truncated": preview_truncated,
        "webLink": item.get("webLink"),
    }


def _compact_email_address(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    address = value.get("emailAddress")
    if not isinstance(address, dict):
        return None
    return {
        "name": str(address.get("name") or "").strip(),
        "address": str(address.get("address") or "").strip(),
    }


def _compact_recipients(value: Any, *, max_items: int = 20) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    recipients: list[dict[str, str]] = []
    for item in value[: int(max_items)]:
        compact = _compact_email_address(item)
        if compact:
            recipients.append(compact)
    return recipients


def _strip_quoted_reply(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    for line in lines:
        normalized = line.strip().lower()
        if normalized.startswith("-----original message-----"):
            break
        if normalized.startswith("from:") and kept:
            break
        if normalized.startswith("on ") and normalized.endswith(" wrote:"):
            break
        kept.append(line)
    return "\n".join(kept).strip()


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


def _client_from_env() -> OutlookClient:
    tenant_id = str(os.environ.get("OUTLOOK_TENANT_ID") or OUTLOOK_TENANT_ID_DEFAULT).strip() or OUTLOOK_TENANT_ID_DEFAULT
    return OutlookClient(
        token=str(os.environ.get("OUTLOOK_TOKEN") or "").strip(),
        graph_base=str(os.environ.get("OUTLOOK_GRAPH_BASE") or OUTLOOK_GRAPH_BASE_DEFAULT).strip() or OUTLOOK_GRAPH_BASE_DEFAULT,
        user_id=str(os.environ.get("OUTLOOK_USER_ID") or OUTLOOK_USER_ID_DEFAULT).strip() or OUTLOOK_USER_ID_DEFAULT,
        refresh_token=str(os.environ.get("OUTLOOK_REFRESH_TOKEN") or "").strip(),
        client_id=str(os.environ.get("OUTLOOK_CLIENT_ID") or "").strip(),
        client_secret=str(os.environ.get("OUTLOOK_CLIENT_SECRET") or "").strip(),
        tenant_id=tenant_id,
        token_uri=str(os.environ.get("OUTLOOK_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        scopes=str(os.environ.get("OUTLOOK_SCOPES") or "").strip(),
    )


def _connector_context() -> dict[str, Any]:
    tenant_id = str(os.environ.get("OUTLOOK_TENANT_ID") or OUTLOOK_TENANT_ID_DEFAULT).strip() or OUTLOOK_TENANT_ID_DEFAULT
    default_user_id = str(os.environ.get("OUTLOOK_USER_ID") or OUTLOOK_USER_ID_DEFAULT).strip() or OUTLOOK_USER_ID_DEFAULT
    return {
        "graph_base": str(os.environ.get("OUTLOOK_GRAPH_BASE") or OUTLOOK_GRAPH_BASE_DEFAULT).strip() or OUTLOOK_GRAPH_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("OUTLOOK_TOKEN") or "").strip()),
        "has_refresh_token": bool(str(os.environ.get("OUTLOOK_REFRESH_TOKEN") or "").strip()),
        "has_client_id": bool(str(os.environ.get("OUTLOOK_CLIENT_ID") or "").strip()),
        "default_user_id": default_user_id,
        "effective_user_id": default_user_id,
        "tenant_id": tenant_id,
        "token_uri": str(os.environ.get("OUTLOOK_TOKEN_URI") or "").strip()
        or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        "capabilities": ["read", "draft", "send"],
    }


mcp = FastMCP(
    "outlook-connector",
    instructions=(
        "Outlook Microsoft 365 connector for inbox search, message inspection, folder discovery, "
        "draft creation, and email sending tasks through Microsoft Graph."
    ),
)


@mcp.tool(description="Return the authenticated Microsoft Graph user profile for token validation.")
def whoami() -> dict[str, Any]:
    return _client_from_env().whoami()


@mcp.tool(description="List Outlook mail folders for the configured mailbox.")
def list_mail_folders(
    top: int = 25,
    include_hidden: bool = False,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_mail_folders(top=top, include_hidden=include_hidden, user_id=user_id)


@mcp.tool(description="List Outlook messages using Microsoft Graph OData filters, search, or paging URLs.")
def list_messages(
    folder_id: str | None = None,
    top: int = 10,
    select: str | None = None,
    filter: str | None = None,
    search: str | None = None,
    order_by: str | None = None,
    page_url: str | None = None,
    body_content_type: str | None = "text",
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_messages(
        folder_id=folder_id,
        top=top,
        select=select,
        filter=filter,
        search=search,
        order_by=order_by,
        page_url=page_url,
        body_content_type=body_content_type,
        user_id=user_id,
    )


@mcp.tool(description="List compact Outlook message summaries for safe mailbox summarization without full message bodies.")
def list_message_summaries(
    folder_id: str | None = None,
    top: int = 20,
    filter: str | None = None,
    search: str | None = None,
    order_by: str | None = "receivedDateTime desc",
    page_url: str | None = None,
    preview_chars: int = 500,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_message_summaries(
        folder_id=folder_id,
        top=top,
        filter=filter,
        search=search,
        order_by=order_by,
        page_url=page_url,
        preview_chars=preview_chars,
        user_id=user_id,
    )


@mcp.tool(description="Get one Outlook message by message ID.")
def get_message(
    message_id: str,
    select: str | None = None,
    body_content_type: str | None = "text",
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().get_message(
        message_id=message_id,
        select=select,
        body_content_type=body_content_type,
        user_id=user_id,
    )


@mcp.tool(description="Get one Outlook message as bounded plain text with optional recipient metadata.")
def get_message_text(
    message_id: str,
    max_body_chars: int = 12_000,
    include_recipients: bool = False,
    strip_quoted_replies: bool = True,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().get_message_text(
        message_id=message_id,
        max_body_chars=max_body_chars,
        include_recipients=include_recipients,
        strip_quoted_replies=strip_quoted_replies,
        user_id=user_id,
    )


@mcp.tool(description="Create an Outlook draft message. Use this when the user wants review before sending.")
def create_draft(
    to: str,
    subject: str,
    body: str = "",
    cc: str | None = None,
    bcc: str | None = None,
    body_html: str | None = None,
    reply_to: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_draft(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        body_html=body_html,
        reply_to=reply_to,
        user_id=user_id,
    )


@mcp.tool(description="Send a new Outlook email immediately. Use this only when the user explicitly asks to send.")
def send_message(
    to: str,
    subject: str,
    body: str = "",
    cc: str | None = None,
    bcc: str | None = None,
    body_html: str | None = None,
    reply_to: str | None = None,
    save_to_sent_items: bool = True,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().send_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        body_html=body_html,
        reply_to=reply_to,
        save_to_sent_items=save_to_sent_items,
        user_id=user_id,
    )


@mcp.tool(description="Send an existing Outlook draft message by ID.")
def send_draft(message_id: str, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().send_draft(message_id=message_id, user_id=user_id)


@mcp.tool(description="Return the Outlook connector runtime context, including configured mailbox user ID.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
