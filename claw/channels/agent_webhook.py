"""Completion webhook of the Agent Channel.

When a turn finishes, the server POSTs the result to the key's callback URL,
so the calling agent need not keep polling (or remember the turn id).

The request is signed: `X-Claw-Signature: sha256=<hex>` is the HMAC-SHA256,
under the key's webhook secret, of `<X-Claw-Timestamp>.<raw body>`. A
receiver recomputes it, compares in constant time, and rejects a timestamp
more than a few minutes old.

The URL is chosen by a user, so every attempt goes through the SSRF guard
(claw/security/ssrf.py): the host must resolve only to public addresses and
the connection is pinned to the address that was checked. Redirects are not
followed. Delivery never blocks or fails the turn: the result is already
stored and can always be polled.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import time
import weakref
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from claw.security.ssrf import UnsafeUrlError, assert_public_url, pinned_client, resolve_public_ips

ATTEMPTS = 3
# Seconds to wait before the 2nd and 3rd attempt.
BACKOFF: Sequence[float] = (2.0, 8.0)
# Whole-attempt budget: DNS lookup, connect, TLS, request and response headers.
TIMEOUT_SECONDS = 5.0
# Deliveries in flight at once in this process; the rest wait their turn.
MAX_CONCURRENT = 8
# A save-time DNS check that takes longer than this is skipped (delivery re-checks).
SAVE_DNS_SECONDS = 2.0
# Host names that never mean a public receiver.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

# One semaphore per event loop: an asyncio.Semaphore binds to the loop it first waits on.
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _slots.get(loop)
    if sem is None:
        sem = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT)
    return sem
MAX_URL_LENGTH = 2048
# A turn's reply can be long; the webhook carries it whole up to this, and
# says so when it was cut (the full reply is always on GET /turns/{id}).
MAX_REPLY_CHARS = 64_000


class InvalidCallbackUrl(ValueError):
    pass


def _is_numeric_host(host: str) -> bool:
    """An IP literal, or a name a resolver reads as one (2130706433, 0x7f.1, 127.1)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    last = host.rstrip(".").rsplit(".", 1)[-1]
    return not any(ch.isalpha() for ch in last) or last.lower().startswith("0x")


async def validate_callback_url(url: str, *, allow_http: bool = False, resolve: bool = True) -> str:
    """Check a callback URL: scheme, length, a real public host name.

    IP literals and local names are refused outright. With `resolve`, the name
    is also looked up (briefly) and refused if it resolves to a non-public
    address; a lookup that fails or is slow is not an error here, because
    every delivery re-checks the resolved address anyway."""
    url = (url or "").strip()
    if len(url) > MAX_URL_LENGTH:
        raise InvalidCallbackUrl("The callback URL is too long.")
    parts = urlsplit(url)
    allowed = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed:
        raise InvalidCallbackUrl("The callback URL must use https.")
    if not parts.hostname:
        raise InvalidCallbackUrl("The callback URL has no host.")
    if parts.username or parts.password:
        raise InvalidCallbackUrl("The callback URL must not contain a user name or password.")
    host = parts.hostname.lower().rstrip(".")
    if (
        _is_numeric_host(host)
        or "." not in host
        or host == "localhost"
        or host.endswith(_LOCAL_SUFFIXES)
    ):
        raise InvalidCallbackUrl("The callback URL must use a public host name, not an IP address or local name.")
    try:
        await assert_public_url(url, resolve=False)
    except UnsafeUrlError as exc:
        raise InvalidCallbackUrl("The callback URL must point to a public address.") from exc
    if resolve:
        try:
            async with asyncio.timeout(SAVE_DNS_SECONDS):
                await resolve_public_ips(url)
        except UnsafeUrlError as exc:
            if "could not resolve" not in str(exc):
                raise InvalidCallbackUrl("The callback URL must point to a public address.") from exc
        except TimeoutError:
            pass
    return url


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def payload(turn: Any) -> dict:
    reply = turn.reply or ""
    return {
        "event": "turn.finished",
        "turn_id": turn.id,
        "conversation_id": turn.conversation_id,
        "status": turn.status,
        "reply": reply[:MAX_REPLY_CHARS],
        "reply_truncated": len(reply) > MAX_REPLY_CHARS,
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
        "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
    }


def _retryable(status: int) -> bool:
    return status >= 500 or status in (408, 425, 429)


async def deliver(
    url: str,
    secret: str,
    body: dict,
    *,
    allow_http: bool = False,
    attempts: int = ATTEMPTS,
    backoff: Sequence[float] = BACKOFF,
    timeout: float = TIMEOUT_SECONDS,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[str, int, str | None]:
    """POST `body` signed; returns (delivered | failed, attempts made, last error).

    Errors are short descriptions (status code or exception class), never the
    URL or the secret, because they are stored and logged."""
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    host = urlsplit(url).hostname or "?"
    error: str | None = None
    made = 0
    try:
        # No DNS here: the pinned transport resolves and checks on every attempt.
        await validate_callback_url(url, allow_http=allow_http, resolve=False)
    except InvalidCallbackUrl as exc:
        return "failed", 0, str(exc)
    make = client_factory or (
        lambda: pinned_client(require_https=not allow_http, timeout=httpx.Timeout(timeout))
    )
    async with make() as http:
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0)
            made += 1
            timestamp = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "PrivateClaw-AgentWebhook/1",
                "X-Claw-Timestamp": timestamp,
                "X-Claw-Signature": sign(secret, timestamp, raw),
                "X-Claw-Turn-Id": str(body.get("turn_id", "")),
                "X-Claw-Delivery-Attempt": str(made),
            }
            try:
                async with _slot():
                    # One budget for the whole attempt: httpx's own timeouts do
                    # not cover the SSRF guard's getaddrinfo.
                    async with asyncio.timeout(timeout):
                        # Streamed and never read: the answer's body is not needed,
                        # and a hostile receiver must not make us buffer a large one.
                        async with http.stream("POST", url, content=raw, headers=headers) as response:
                            status = response.status_code
            except UnsafeUrlError:
                error = "The callback host resolves to a non-public address."
                break
            except TimeoutError:
                error = "Timeout"
                continue
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                continue
            if 200 <= status < 300:
                logger.info("Agent webhook for turn {} delivered to {} (attempt {})", body.get("turn_id"), host, made)
                return "delivered", made, None
            error = f"HTTP {status}"
            if not _retryable(status):
                break
    logger.warning(
        "Agent webhook for turn {} to {} failed after {} attempt(s): {}", body.get("turn_id"), host, made, error
    )
    return "failed", made, error
