"""Forgot-password flow: generic response (no account-enumeration oracle),
detached send, and atomic single-use token redemption via compare-and-swap."""

from datetime import datetime, timezone

from claw.auth.password_reset import make_password_reset_token, verify_password_reset_token
from tests.conftest_app import build_api_app, client

SECRET = "test-secret"


def test_password_reset_token_roundtrip():
    token = make_password_reset_token("user-1", "nonce-abc", SECRET, 3600)
    assert verify_password_reset_token(token, SECRET) == ("user-1", "nonce-abc")


def test_password_reset_token_rejects_wrong_purpose_and_secret():
    from claw.auth.tokens import create_access_token

    bearer = create_access_token("user-1", SECRET)
    assert verify_password_reset_token(bearer, SECRET) is None
    token = make_password_reset_token("user-1", "nonce-abc", SECRET, 3600)
    assert verify_password_reset_token(token, "other-secret") is None


async def test_forgot_password_returns_generic_ok_regardless_of_email(db_factory):
    """Same {"ok": true} response whether the email belongs to a real
    password account or doesn't exist at all — revealing that distinction
    would be an account-enumeration oracle."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        await c.post("/api/auth/register", json={"email": "real@x.io", "password": "password123"})

        real = await c.post("/api/auth/forgot-password", json={"email": "real@x.io"})
        fake = await c.post("/api/auth/forgot-password", json={"email": "nobody-here@x.io"})
        assert real.status_code == fake.status_code == 200
        assert real.json() == fake.json() == {"ok": True}


async def test_forgot_password_does_not_apply_to_imported_pending_account(db_factory):
    """An imported user with no password yet has nothing to "forget" — must
    use the activation flow instead, not password reset."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        user = await app.state.claw.users.create(
            email="pending@x.io", password_hash="", signup_method="imported"
        )
        r = await c.post("/api/auth/forgot-password", json={"email": "pending@x.io"})
        assert r.status_code == 200  # still generic — doesn't reveal anything
        # No reset was actually claimed/sent for this account.
        refreshed = await app.state.claw.users.get(user.id)
        assert refreshed.password_reset_nonce is None
        assert refreshed.password_reset_sent_at is None


async def test_reset_password_roundtrip_and_single_use(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "reset@x.io", "password": "oldpassword1"})
        uid = reg.json()["user"]["id"]

        now = datetime.now(timezone.utc)
        nonce = "test-nonce-123"
        claimed = await app.state.claw.users.claim_password_reset_send(uid, now, 300, nonce)
        assert claimed is True
        token = make_password_reset_token(uid, nonce, app.state.claw.settings.secret_key, 3600)

        r = await c.post("/api/auth/reset-password", json={"token": token, "password": "newpassword1"})
        assert r.status_code == 200

        # New password works via the ordinary login path.
        login = await c.post("/api/auth/login", json={"email": "reset@x.io", "password": "newpassword1"})
        assert login.status_code == 200

        # Old password no longer works.
        old_login = await c.post("/api/auth/login", json={"email": "reset@x.io", "password": "oldpassword1"})
        assert old_login.status_code == 401

        # The same token cannot be redeemed twice (nonce was cleared by the CAS).
        replay = await c.post("/api/auth/reset-password", json={"token": token, "password": "anotherpassword1"})
        assert replay.status_code == 400


async def test_reset_password_rejects_bad_token(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        r = await c.post(
            "/api/auth/reset-password", json={"token": "not-a-real-token", "password": "password123"}
        )
        assert r.status_code == 400


async def test_reset_password_requires_matching_nonce(db_factory):
    """A syntactically-valid, correctly-signed token whose nonce doesn't
    match what's currently stored (e.g. superseded by a newer request, or
    never actually issued) must not redeem — the DB row, not just the
    signature, is the source of truth for validity."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "nomatch@x.io", "password": "password123"})
        uid = reg.json()["user"]["id"]
        # Never actually claimed via claim_password_reset_send, so
        # password_reset_nonce is still None on this row.
        token = make_password_reset_token(uid, "unclaimed-nonce", app.state.claw.settings.secret_key, 3600)
        r = await c.post("/api/auth/reset-password", json={"token": token, "password": "newpassword1"})
        assert r.status_code == 400


async def test_reset_password_never_writes_password_for_suspended_account(db_factory):
    """A suspended account's password must not change even though a valid,
    unredeemed reset token exists — the atomic redeem is gated on is_active
    in the same UPDATE, not just rejected after the fact (see
    UserStore.redeem_password_reset)."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "suspended@x.io", "password": "oldpassword1"})
        uid = reg.json()["user"]["id"]
        await app.state.claw.users.update_flags(uid, is_active=False)

        now = datetime.now(timezone.utc)
        nonce = "susp-nonce"
        await app.state.claw.users.claim_password_reset_send(uid, now, 300, nonce)
        token = make_password_reset_token(uid, nonce, app.state.claw.settings.secret_key, 3600)

        r = await c.post("/api/auth/reset-password", json={"token": token, "password": "newpassword1"})
        assert r.status_code == 403
        # The nonce must still be intact too (redeem_password_reset's WHERE
        # clause matched nothing, so it cleared nothing) — proving this was
        # rejected atomically, not written-then-blocked.
        assert (await app.state.claw.users.get(uid)).password_reset_nonce == nonce

        # Reactivate and confirm the OLD password still works — the reset
        # attempt against the suspended account genuinely never wrote
        # anything, unlike the pre-fix behavior where it silently did.
        await app.state.claw.users.update_flags(uid, is_active=True)
        old_login = await c.post("/api/auth/login", json={"email": "suspended@x.io", "password": "oldpassword1"})
        assert old_login.status_code == 200
        new_login = await c.post("/api/auth/login", json={"email": "suspended@x.io", "password": "newpassword1"})
        assert new_login.status_code == 401


async def test_reset_password_bad_token_and_suspended_account_get_different_errors(db_factory):
    """The frontend needs to tell these apart (an accurate 'account
    suspended' message vs a generic 'invalid or expired link') — both must
    be reachable and distinguishable via status code."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "susp2@x.io", "password": "oldpassword1"})
        uid = reg.json()["user"]["id"]
        await app.state.claw.users.update_flags(uid, is_active=False)

        now = datetime.now(timezone.utc)
        nonce = "susp2-nonce"
        await app.state.claw.users.claim_password_reset_send(uid, now, 300, nonce)
        token = make_password_reset_token(uid, nonce, app.state.claw.settings.secret_key, 3600)

        suspended = await c.post("/api/auth/reset-password", json={"token": token, "password": "newpassword1"})
        assert suspended.status_code == 403
        assert suspended.json()["detail"] == "account suspended"

        bad = await c.post(
            "/api/auth/reset-password", json={"token": "not-a-real-token", "password": "newpassword1"}
        )
        assert bad.status_code == 400
        assert bad.json()["detail"] == "invalid or expired reset link"
