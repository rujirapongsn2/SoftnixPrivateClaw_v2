"""Signed, stateless tokens for the imported-user email-activation link.

Mirrors claw/auth/oidc.py's make_state/verify_state: reuses the same stdlib
HS256 encode/decode_access_token as bearer access tokens, distinguished only
by a "purpose" claim (decode_access_token doesn't enforce one itself, so
every caller that mints a special-purpose token is responsible for checking
its own claim shape after decoding — same convention oidc.py already uses).

Deliberately stateless: there is no DB row tracking "used" tokens. Redemption
(claw/api/auth.py's complete_registration) re-checks signup_method=="imported"
and password_hash=="" at the DB layer, which is naturally false after the
first successful use (or if the account is suspended/deleted meanwhile) — so
a replayed or stale token is inert with no extra storage needed.
"""

import secrets

from claw.auth.tokens import TokenError, decode_access_token, encode


def make_activation_token(user_id: str, secret: str, ttl_seconds: int) -> str:
    return encode({"purpose": "activation", "uid": user_id, "n": secrets.token_urlsafe(8)}, secret, ttl_seconds)


def verify_activation_token(token: str, secret: str) -> str | None:
    """Returns the user id the token was minted for, or None if the token is
    missing, malformed, expired, or not an activation token."""
    try:
        payload = decode_access_token(token, secret)
    except TokenError:
        return None
    uid = payload.get("uid")
    if payload.get("purpose") != "activation" or not isinstance(uid, str):
        return None
    return uid
