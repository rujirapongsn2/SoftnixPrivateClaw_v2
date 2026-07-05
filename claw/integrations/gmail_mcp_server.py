"""Gmail MCP server for the built-in Gmail connector preset."""

from __future__ import annotations

import base64
from base64 import urlsafe_b64encode
from html import unescape
from email.message import EmailMessage
from email.utils import format_datetime
from email.policy import SMTP
from datetime import datetime, timezone
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

GMAIL_API_BASE_DEFAULT = "https://gmail.googleapis.com/gmail/v1"
GMAIL_USER_ID_DEFAULT = "me"
GMAIL_USER_AGENT = "nanobot-gmail-connector/1.0"
GMAIL_WRITE_SCOPES = {
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
}


@dataclass
class GmailClient:
    """Small Gmail REST API client used by the MCP server and validation flow."""

    token: str
    api_base: str = GMAIL_API_BASE_DEFAULT
    user_id: str = GMAIL_USER_ID_DEFAULT
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
                "User-Agent": GMAIL_USER_AGENT,
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
            raise ValueError("Gmail access token is required")
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

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        resolved = str(user_id or self.user_id or GMAIL_USER_ID_DEFAULT).strip()
        return resolved or GMAIL_USER_ID_DEFAULT

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", f"/users/{self._resolve_user_id()}/profile")

    def list_messages(
        self,
        query: str = "",
        *,
        label_ids: list[str] | None = None,
        max_results: int = 10,
        page_token: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": str(query or "").strip(),
            "maxResults": int(max_results),
        }
        if label_ids:
            params["labelIds"] = [str(item).strip() for item in label_ids if str(item or "").strip()]
        if page_token:
            params["pageToken"] = str(page_token).strip()
        return self._request("GET", f"/users/{self._resolve_user_id(user_id)}/messages", params=params)

    def get_message(self, message_id: str, *, format: str = "full", user_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/users/{self._resolve_user_id(user_id)}/messages/{str(message_id).strip()}",
            params={"format": format},
        )

    def get_message_digest(
        self,
        message_id: str,
        *,
        user_id: str | None = None,
        body_chars: int = 400,
    ) -> dict[str, Any]:
        message = self.get_message(message_id, format="full", user_id=user_id)
        return _build_message_digest(message, body_chars=body_chars)

    def get_message_digests(
        self,
        message_ids: list[str],
        *,
        user_id: str | None = None,
        body_chars: int = 400,
    ) -> dict[str, Any]:
        digests: list[dict[str, Any]] = []
        for raw_message_id in message_ids:
            message_id = str(raw_message_id or "").strip()
            if not message_id:
                continue
            digests.append(self.get_message_digest(message_id, user_id=user_id, body_chars=body_chars))
        return {
            "messages": digests,
            "count": len(digests),
        }

    def get_thread(self, thread_id: str, *, format: str = "full", user_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/users/{self._resolve_user_id(user_id)}/threads/{str(thread_id).strip()}",
            params={"format": format},
        )

    def list_labels(self, user_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", f"/users/{self._resolve_user_id(user_id)}/labels")

    def token_scopes(self) -> set[str]:
        data = self._tokeninfo()
        scopes = str(data.get("scope") or "").split()
        return {scope.strip() for scope in scopes if scope.strip()}

    def _tokeninfo(self) -> dict[str, Any]:
        with httpx.Client(
            timeout=20.0,
            transport=self.transport,
            headers={"Accept": "application/json", "User-Agent": GMAIL_USER_AGENT},
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
            headers={"Accept": "application/json", "User-Agent": GMAIL_USER_AGENT},
        ) as client:
            response = client.post(
                token_uri,
                data=form_data,
            )
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
            raise ValueError("Gmail token refresh did not return an access token")
        self.token = new_token
        returned_refresh = str(payload.get("refresh_token") or "").strip()
        if returned_refresh:
            self.refresh_token = returned_refresh
        return True

    def has_refresh_credentials(self) -> bool:
        return bool(str(self.refresh_token or "").strip() and str(self.client_id or "").strip())

    def ensure_write_scope(self) -> set[str]:
        scopes = self.token_scopes()
        if scopes.intersection(GMAIL_WRITE_SCOPES):
            return scopes
        raise ValueError(
            "Gmail token does not include a write scope. Regenerate the token with gmail.compose or gmail.send "
            "for draft/send support."
        )

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
        thread_id: str | None = None,
        user_id: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._build_message_payload(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            body_html=body_html,
            reply_to=reply_to,
            thread_id=thread_id,
            from_address=self._resolve_from_address(user_id),
            attachments=attachments,
        )
        attachment_count = int(payload.pop("_attachment_count", 0))
        self.ensure_write_scope()
        result = self._request(
            "POST",
            f"/users/{self._resolve_user_id(user_id)}/drafts",
            json_data={"message": payload},
        )
        if isinstance(result, dict):
            result["attachment_count"] = attachment_count
        return result

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
        thread_id: str | None = None,
        user_id: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._build_message_payload(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            body_html=body_html,
            reply_to=reply_to,
            thread_id=thread_id,
            from_address=self._resolve_from_address(user_id),
            attachments=attachments,
        )
        attachment_count = int(payload.pop("_attachment_count", 0))
        self.ensure_write_scope()
        result = self._request(
            "POST",
            f"/users/{self._resolve_user_id(user_id)}/messages/send",
            json_data=payload,
        )
        if isinstance(result, dict):
            result["attachment_count"] = attachment_count
        return result

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
        thread_id: str | None = None,
        from_address: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        message = EmailMessage()
        to_list = _normalize_recipients(to)
        cc_list = _normalize_recipients(cc)
        bcc_list = _normalize_recipients(bcc)
        if not to_list:
            raise ValueError("Gmail message recipient is required")
        message["To"] = ", ".join(to_list)
        if cc_list:
            message["Cc"] = ", ".join(cc_list)
        if bcc_list:
            message["Bcc"] = ", ".join(bcc_list)
        if from_address:
            message["From"] = str(from_address).strip()
        if reply_to:
            message["Reply-To"] = str(reply_to).strip()
        message["Subject"] = str(subject or "").strip()
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        if body_html:
            message.set_content(str(body or ""))
            message.add_alternative(str(body_html), subtype="html")
        else:
            message.set_content(str(body or ""))
        resolved_attachments = _resolve_attachment_paths(attachments)
        for attachment_path in resolved_attachments:
            mime_type, _ = mimetypes.guess_type(str(attachment_path))
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            message.add_attachment(
                attachment_path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=attachment_path.name,
            )
        raw = urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode("ascii")
        payload: dict[str, Any] = {"raw": raw, "_attachment_count": len(resolved_attachments)}
        if thread_id:
            payload["threadId"] = str(thread_id).strip()
        return payload

    def _resolve_from_address(self, user_id: str | None = None) -> str | None:
        resolved = str(user_id or self.user_id or "").strip()
        if resolved and "@" in resolved:
            return resolved
        try:
            profile = self.whoami()
        except Exception:
            return resolved or None
        email_address = str(profile.get("emailAddress") or "").strip()
        if email_address:
            return email_address
        return resolved or None


def _normalize_recipients(value: str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = [str(value)]
    return [item.strip() for item in items if item and item.strip()]


def _resolve_attachment_paths(attachments: list[str] | None) -> list[Path]:
    resolved: list[Path] = []
    for item in attachments or []:
        raw = str(item or "").strip()
        if not raw:
            continue
        path = _resolve_attachment_path(raw)
        if path is None:
            raise FileNotFoundError(f"Gmail attachment not found: {raw}")
        if not path.is_file():
            raise FileNotFoundError(f"Gmail attachment is not a file: {raw}")
        resolved.append(path)
    return resolved


def _resolve_attachment_path(raw: str) -> Path | None:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() and candidate.exists():
        return candidate.resolve()

    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.append((cwd / candidate).resolve())

    runtime_path = Path(__file__).resolve()
    instance_root = runtime_path.parent.parent
    workspace_root = instance_root / "workspace"
    candidates.append((workspace_root / candidate).resolve())

    if raw.startswith("workspace/"):
        without_workspace = raw[len("workspace/") :]
        candidates.append((cwd / without_workspace).resolve())
        candidates.append((workspace_root / without_workspace).resolve())

    for path in candidates:
        if path.exists():
            return path
    return None


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
            return "; ".join(messages).strip()
    return ""


def _payload_headers(payload: dict[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in (payload or {}).get("headers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        headers[name.lower()] = str(item.get("value") or "").strip()
    return headers


def _walk_payload_parts(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    parts: list[dict[str, Any]] = [payload]
    for item in payload.get("parts") or []:
        if isinstance(item, dict):
            parts.extend(_walk_payload_parts(item))
    return parts


def _decode_body_data(data: str) -> str:
    raw = str(data or "").strip()
    if not raw:
        return ""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", value)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _extract_body_excerpt(payload: dict[str, Any] | None, *, body_chars: int) -> str:
    if body_chars <= 0:
        return ""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in _walk_payload_parts(payload):
        mime_type = str(part.get("mimeType") or "").strip().lower()
        body = part.get("body") or {}
        data = _decode_body_data(str(body.get("data") or ""))
        if not data:
            continue
        if mime_type == "text/plain":
            plain_parts.append(data)
        elif mime_type == "text/html":
            html_parts.append(_strip_html(data))
    combined = "\n".join(item.strip() for item in plain_parts if item.strip()).strip()
    if not combined:
        combined = "\n".join(item.strip() for item in html_parts if item.strip()).strip()
    if len(combined) <= body_chars:
        return combined
    return combined[:body_chars].rstrip() + "..."


def _attachment_names(payload: dict[str, Any] | None, *, limit: int = 10) -> list[str]:
    names: list[str] = []
    for part in _walk_payload_parts(payload):
        filename = str(part.get("filename") or "").strip()
        if not filename:
            continue
        names.append(filename)
        if len(names) >= limit:
            break
    return names


def _build_message_digest(message: dict[str, Any], *, body_chars: int = 400) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message, dict) else {}
    headers = _payload_headers(payload if isinstance(payload, dict) else {})
    attachment_names = _attachment_names(payload if isinstance(payload, dict) else {})
    return {
        "id": str(message.get("id") or "").strip(),
        "threadId": str(message.get("threadId") or "").strip(),
        "labelIds": list(message.get("labelIds") or []),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "date": headers.get("date", ""),
        "snippet": str(message.get("snippet") or "").strip(),
        "hasAttachments": bool(attachment_names),
        "attachmentCount": len(attachment_names),
        "attachmentNames": attachment_names,
        "bodyExcerpt": _extract_body_excerpt(payload if isinstance(payload, dict) else {}, body_chars=body_chars),
    }


def _client_from_env() -> GmailClient:
    return GmailClient(
        token=str(os.environ.get("GMAIL_TOKEN") or "").strip(),
        api_base=str(os.environ.get("GMAIL_API_BASE") or GMAIL_API_BASE_DEFAULT).strip() or GMAIL_API_BASE_DEFAULT,
        user_id=str(os.environ.get("GMAIL_USER_ID") or GMAIL_USER_ID_DEFAULT).strip() or GMAIL_USER_ID_DEFAULT,
        refresh_token=str(os.environ.get("GMAIL_REFRESH_TOKEN") or "").strip(),
        client_id=str(os.environ.get("GMAIL_CLIENT_ID") or "").strip(),
        client_secret=str(os.environ.get("GMAIL_CLIENT_SECRET") or "").strip(),
        token_uri=str(os.environ.get("GMAIL_TOKEN_URI") or "https://oauth2.googleapis.com/token").strip() or "https://oauth2.googleapis.com/token",
    )


def _connector_context() -> dict[str, Any]:
    default_user_id = str(os.environ.get("GMAIL_USER_ID") or "").strip() or GMAIL_USER_ID_DEFAULT
    return {
        "api_base": str(os.environ.get("GMAIL_API_BASE") or GMAIL_API_BASE_DEFAULT).strip() or GMAIL_API_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("GMAIL_TOKEN") or "").strip()),
        "has_refresh_token": bool(str(os.environ.get("GMAIL_REFRESH_TOKEN") or "").strip()),
        "has_client_id": bool(str(os.environ.get("GMAIL_CLIENT_ID") or "").strip()),
        "default_user_id": default_user_id,
        "effective_user_id": default_user_id,
        "token_uri": str(os.environ.get("GMAIL_TOKEN_URI") or "https://oauth2.googleapis.com/token").strip() or "https://oauth2.googleapis.com/token",
        "capabilities": ["read", "draft", "send"],
    }


mcp = FastMCP(
    "gmail-connector",
    instructions=(
        "Gmail connector for inbox search, message inspection, thread reading, label discovery, draft creation, and email sending tasks. "
        "Use the tools for structured Gmail access instead of ad-hoc scraping."
    ),
)


@mcp.tool(description="Return the authenticated Gmail user profile for token validation.")
def whoami() -> dict[str, Any]:
    return _client_from_env().whoami()


@mcp.tool(description="List Gmail messages using Gmail query syntax. If user_id is omitted, use the configured default user ID.")
def list_messages(
    query: str = "",
    label_ids: list[str] | None = None,
    max_results: int = 10,
    page_token: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().list_messages(
        query=query,
        label_ids=label_ids,
        max_results=max_results,
        page_token=page_token,
        user_id=user_id,
    )


@mcp.tool(description="Get one Gmail message by message ID. If user_id is omitted, use the configured default user ID.")
def get_message(message_id: str, format: str = "full", user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_message(message_id=message_id, format=format, user_id=user_id)


@mcp.tool(description="Get a compact digest for one Gmail message by message ID. Use this for triage and summaries before fetching full message bodies.")
def get_message_digest(message_id: str, body_chars: int = 400, user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_message_digest(message_id=message_id, body_chars=body_chars, user_id=user_id)


@mcp.tool(description="Get compact digests for multiple Gmail messages in one tool call. Prefer this over multiple full get_message calls when triaging or summarizing inbox results.")
def get_message_digests(
    message_ids: list[str],
    body_chars: int = 400,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().get_message_digests(message_ids=message_ids, body_chars=body_chars, user_id=user_id)


@mcp.tool(description="Get one Gmail thread by thread ID. If user_id is omitted, use the configured default user ID.")
def get_thread(thread_id: str, format: str = "full", user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().get_thread(thread_id=thread_id, format=format, user_id=user_id)


@mcp.tool(description="List labels for the configured Gmail mailbox.")
def list_labels(user_id: str | None = None) -> dict[str, Any]:
    return _client_from_env().list_labels(user_id=user_id)


@mcp.tool(description="Create a Gmail draft message. Use this when the user wants to prepare email without sending it yet. Pass local file paths in attachments to attach files.")
def create_draft(
    to: str,
    subject: str,
    body: str = "",
    cc: str | None = None,
    bcc: str | None = None,
    body_html: str | None = None,
    reply_to: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_draft(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        body_html=body_html,
        reply_to=reply_to,
        thread_id=thread_id,
        user_id=user_id,
        attachments=attachments,
    )


@mcp.tool(description="Send a Gmail message immediately. Use this when the user explicitly asks to send email. Pass local file paths in attachments to attach files.")
def send_message(
    to: str,
    subject: str,
    body: str = "",
    cc: str | None = None,
    bcc: str | None = None,
    body_html: str | None = None,
    reply_to: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    return _client_from_env().send_message(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        body_html=body_html,
        reply_to=reply_to,
        thread_id=thread_id,
        user_id=user_id,
        attachments=attachments,
    )


@mcp.tool(description="Return the Gmail connector runtime context, including configured default mailbox user ID.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
