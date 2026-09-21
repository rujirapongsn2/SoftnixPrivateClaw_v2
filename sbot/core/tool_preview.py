"""Safe, bounded tool-call previews for user-visible activity feeds."""

from __future__ import annotations

import json
import re
from typing import Any


_SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "api_key",
    "cookie",
    "credential",
    "credentials",
    "otp",
    "passcode",
    "password",
    "private_key",
    "secret",
    "token",
    "refresh_token",
}
_SENSITIVE_SUFFIXES = ("_api_key", "_authorization", "_cookie", "_password", "_secret", "_token")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    # Match the complete identifier, not only a bare ``token``/``secret``.
    # Credentials commonly arrive in shell output as OPENAI_API_KEY,
    # oauth_access_token or client-secret; a word boundary before the sensitive
    # suffix misses every one of those names because ``_`` is a word character.
    r"(?i)(?<![A-Za-z0-9])"
    r"([A-Za-z0-9_-]*(?:api[-_]?key|authorization|cookie|credential|otp|passcode|password|"
    r"private[-_]?key|access[-_]?token|refresh[-_]?token|secret|token))"
    r"(\s*[:=]\s*)"
    r"((?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+))"
)
_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|token)=)[^&#\s]+"
)


def _sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _redact_text(value: str) -> str:
    value = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", value)
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    return _QUERY_RE.sub(lambda m: f"{m.group(1)}[redacted]", value)


def redact_tool_data(value: Any) -> Any:
    """Recursively hide credentials while preserving useful arguments such as URLs."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _sensitive_key(key) else redact_tool_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_tool_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_tool_data(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def safe_args_preview(arguments: dict[str, Any], max_chars: int = 200) -> str:
    text = json.dumps(redact_tool_data(arguments), ensure_ascii=False)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def safe_text_preview(value: str | None, max_chars: int = 500) -> str:
    """Redact labelled secrets from a plain-text tool result and bound storage."""
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        text = _redact_text(text)
    else:
        text = json.dumps(redact_tool_data(parsed), ensure_ascii=False)
    return text[:max_chars] + ("…" if len(text) > max_chars else "")
