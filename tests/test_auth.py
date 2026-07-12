"""Password auth + JWT issuance and verification."""

import time

import pytest

from claw.auth.passwords import hash_password, verify_password
from claw.auth.tokens import TokenError, create_access_token, decode_access_token
from tests.conftest_app import build_api_app, client


def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)
    # Two hashes of the same password differ (random salt).
    assert h != hash_password("correct horse battery staple")


def test_jwt_roundtrip_and_tamper():
    token = create_access_token("user-123", "secret")
    assert decode_access_token(token, "secret")["sub"] == "user-123"
    with pytest.raises(TokenError):
        decode_access_token(token, "other-secret")
    with pytest.raises(TokenError):
        decode_access_token(token + "x", "secret")


def test_jwt_expiry():
    token = create_access_token("u", "secret", expires_seconds=-1)
    with pytest.raises(TokenError):
        decode_access_token(token, "secret")


async def test_first_user_becomes_admin_others_do_not(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        r1 = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        assert r1.status_code == 200
        assert r1.json()["user"]["is_admin"] is True

        r2 = await c.post("/api/auth/register", json={"email": "b@x.io", "password": "password123"})
        assert r2.status_code == 200
        assert r2.json()["user"]["is_admin"] is False


async def test_login_and_authenticated_request(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        r = await c.post("/api/auth/login", json={"email": "a@x.io", "password": "password123"})
        assert r.status_code == 200
        token = r.json()["access_token"]

        # Use the JWT as a bearer token on a protected endpoint.
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["email"] == "a@x.io"

        bad = await c.post("/api/auth/login", json={"email": "a@x.io", "password": "nope"})
        assert bad.status_code == 401


async def test_duplicate_registration_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        dup = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        assert dup.status_code == 409


async def test_suspended_user_cannot_authenticate(db_factory, stores):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        token = reg.json()["access_token"]
        uid = reg.json()["user"]["id"]
        # Suspend directly, then the bearer token must stop working.
        await app.state.claw.users.update_flags(uid, is_active=False)
        r = await c.get("/api/skills", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


async def test_closed_registration_blocks_second_user(db_factory):
    app = build_api_app(db_factory, open_registration=False)
    async with client(app) as c:
        r1 = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        assert r1.status_code == 200  # first user always allowed (bootstrap)
        r2 = await c.post("/api/auth/register", json={"email": "b@x.io", "password": "password123"})
        assert r2.status_code == 403


async def test_me_reports_has_password(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "password123"})
        token = reg.json()["access_token"]
        assert reg.json()["user"]["has_password"] is True
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["has_password"] is True


async def test_change_password_requires_current_password(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "a@x.io", "password": "oldpassword1"})
        token = reg.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        wrong = await c.post(
            "/api/auth/change-password",
            json={"current_password": "nope", "new_password": "newpassword1"},
            headers=headers,
        )
        # 403, not 401 — a 401 here would make the frontend's global
        # "401 means the session is invalid" handling force-log-out a user
        # who simply mistyped their current password.
        assert wrong.status_code == 403

        ok = await c.post(
            "/api/auth/change-password",
            json={"current_password": "oldpassword1", "new_password": "newpassword1"},
            headers=headers,
        )
        assert ok.status_code == 200

        # Old password no longer works; new one does.
        old_login = await c.post("/api/auth/login", json={"email": "a@x.io", "password": "oldpassword1"})
        assert old_login.status_code == 401
        new_login = await c.post("/api/auth/login", json={"email": "a@x.io", "password": "newpassword1"})
        assert new_login.status_code == 200


async def test_change_password_rejected_for_account_without_password(db_factory):
    """An imported-pending user (no password yet) has nothing to "change" —
    must use the activation flow instead."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        user = await app.state.claw.users.create(
            email="pending@x.io", password_hash="", signup_method="imported"
        )
        # Mint a bearer token directly for this user (bypassing login, which
        # can't succeed for a passwordless account) to exercise the endpoint.
        token = create_access_token(user.id, app.state.claw.settings.secret_key)
        r = await c.post(
            "/api/auth/change-password",
            json={"current_password": "anything", "new_password": "newpassword1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
