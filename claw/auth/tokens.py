"""Minimal HS256 JWT (stdlib only). Enough for bearer access tokens; no deps."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class TokenError(Exception):
    pass


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _sign(signing_input: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64url_encode(sig)


def encode(payload: dict[str, Any], secret: str, expires_seconds: int) -> str:
    """Sign an arbitrary claim set as an HS256 JWT with iat/exp."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    body = {**payload, "iat": now, "exp": now + expires_seconds}
    header_seg = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_seg = _b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header_seg}.{payload_seg}".encode()
    return f"{header_seg}.{payload_seg}.{_sign(signing_input, secret)}"


def create_access_token(subject: str, secret: str, expires_seconds: int = 7 * 24 * 3600) -> str:
    return encode({"sub": subject}, secret, expires_seconds)


def decode_unverified(token: str) -> dict[str, Any]:
    """Read a JWT payload WITHOUT signature verification.

    Only safe for tokens already trusted via a secure channel (e.g. an OIDC
    id_token received over TLS directly from the provider's token endpoint).
    """
    try:
        _, payload_seg, _ = token.split(".")
        return json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError):
        return {}


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    try:
        header_seg, payload_seg, sig_seg = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc

    signing_input = f"{header_seg}.{payload_seg}".encode()
    expected = _sign(signing_input, secret)
    if not hmac.compare_digest(expected, sig_seg):
        raise TokenError("bad signature")

    try:
        payload = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("bad payload") from exc

    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("token expired")
    return payload
