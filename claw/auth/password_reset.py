"""Signed tokens for the "forgot password" email-reset link.

Mirrors claw/auth/activation.py's pattern (stdlib HS256 encode/decode_access_token,
distinguished by a "purpose" claim) with one difference: activation's token is
stateless because password_hash=="" naturally goes false after first use.
Resetting a password has no such natural signal (the account has a password
hash both before and after), so this token carries a caller-supplied nonce
that's also persisted in UserStore.claim_password_reset_send() at issuance
time and cleared via UserStore.redeem_password_reset()'s atomic
compare-and-swap at redemption time — giving proper single-use enforcement
with a single row and no extra table.
"""

from claw.auth.tokens import TokenError, decode_access_token, encode


def make_password_reset_token(user_id: str, nonce: str, secret: str, ttl_seconds: int) -> str:
    return encode({"purpose": "password_reset", "uid": user_id, "n": nonce}, secret, ttl_seconds)


def verify_password_reset_token(token: str, secret: str) -> tuple[str, str] | None:
    """Returns (user_id, nonce) the token was minted for, or None if the
    token is missing, malformed, expired, or not a password-reset token."""
    try:
        payload = decode_access_token(token, secret)
    except TokenError:
        return None
    uid = payload.get("uid")
    nonce = payload.get("n")
    if payload.get("purpose") != "password_reset" or not isinstance(uid, str) or not isinstance(nonce, str):
        return None
    return uid, nonce
