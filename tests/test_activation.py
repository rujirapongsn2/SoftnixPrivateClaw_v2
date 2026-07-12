"""Imported-user email-verified activation: closes the account-enumeration /
pre-emptive-takeover gap where complete-registration used to trust a bare
email match (see claw/api/auth.py)."""

from claw.auth.activation import make_activation_token, verify_activation_token
from tests.conftest_app import build_api_app, client

SECRET = "test-secret"


async def _make_imported_user(app, email="jane@x.io", display_name="Jane Doe", is_active=True):
    user = await app.state.claw.users.create(
        email=email, password_hash="", display_name=display_name, signup_method="imported"
    )
    if not is_active:
        await app.state.claw.users.update_flags(user.id, is_active=False)
    return user


def test_activation_token_roundtrip():
    token = make_activation_token("user-1", SECRET, 3600)
    assert verify_activation_token(token, SECRET) == "user-1"


def test_activation_token_rejects_wrong_purpose_and_secret():
    # A normal bearer token (sub-only, no purpose claim) must never verify as
    # an activation token, and a valid activation token signed with a
    # different secret must not verify either.
    from claw.auth.tokens import create_access_token

    bearer = create_access_token("user-1", SECRET)
    assert verify_activation_token(bearer, SECRET) is None
    activation = make_activation_token("user-1", SECRET, 3600)
    assert verify_activation_token(activation, "other-secret") is None


async def test_login_for_imported_pending_user_gets_generic_401(db_factory):
    """The old behavior (403 + registration_incomplete reason) was an
    account-enumeration oracle — an imported-but-not-activated account must
    now be indistinguishable from a wrong password."""
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _make_imported_user(app)
        r = await c.post("/api/auth/login", json={"email": "jane@x.io", "password": "whatever1"})
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid email or password"

        # Same status/body shape as an ordinary wrong-password failure.
        await c.post("/api/auth/register", json={"email": "normal@x.io", "password": "password123"})
        wrong = await c.post("/api/auth/login", json={"email": "normal@x.io", "password": "wrong"})
        assert wrong.status_code == r.status_code
        assert wrong.json()["detail"] == r.json()["detail"]


async def test_register_for_imported_pending_email_gets_generic_409(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _make_imported_user(app)
        r = await c.post("/api/auth/register", json={"email": "jane@x.io", "password": "password123"})
        assert r.status_code == 409
        assert r.json()["detail"] == "email already registered"


async def test_activation_info_and_complete_registration(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        user = await _make_imported_user(app)
        token = make_activation_token(user.id, app.state.claw.settings.secret_key, 3600)

        info = await c.post("/api/auth/activation", json={"token": token})
        assert info.status_code == 200
        assert info.json() == {"email": "jane@x.io", "display_name": "Jane Doe"}

        done = await c.post(
            "/api/auth/complete-registration",
            json={"token": token, "password": "newpassword1", "display_name": "Jane D."},
        )
        assert done.status_code == 200
        assert done.json()["user"]["display_name"] == "Jane D."

        # Now a normal login with the new password works via the ordinary path.
        login = await c.post("/api/auth/login", json={"email": "jane@x.io", "password": "newpassword1"})
        assert login.status_code == 200

        # The same token cannot be redeemed twice (password_hash is now set).
        replay = await c.post(
            "/api/auth/complete-registration",
            json={"token": token, "password": "anotherpassword1"},
        )
        assert replay.status_code == 400


async def test_complete_registration_rejects_bad_token(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        r = await c.post(
            "/api/auth/complete-registration", json={"token": "not-a-real-token", "password": "password123"}
        )
        assert r.status_code == 400
        info = await c.post("/api/auth/activation", json={"token": "not-a-real-token"})
        assert info.status_code == 400


async def test_complete_registration_blocks_suspended_account(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        user = await _make_imported_user(app, is_active=False)
        token = make_activation_token(user.id, app.state.claw.settings.secret_key, 3600)
        r = await c.post(
            "/api/auth/complete-registration", json={"token": token, "password": "password123"}
        )
        assert r.status_code == 403


async def test_resend_activation_requires_email_enabled(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_reg = await c.post("/api/auth/register", json={"email": "admin@x.io", "password": "password123"})
        admin_token = admin_reg.json()["access_token"]
        user = await _make_imported_user(app)

        # No SMTP configured yet — a clear, actionable error, not a silent no-op.
        r = await c.post(
            f"/api/admin/users/{user.id}/resend-activation",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 422

        # Not a pending-imported user (already has a password) -> 400.
        other = await c.post("/api/auth/register", json={"email": "other@x.io", "password": "password123"})
        other_id = other.json()["user"]["id"]
        r2 = await c.post(
            f"/api/admin/users/{other_id}/resend-activation",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r2.status_code == 400


async def test_resend_activation_reports_true_outcome(db_factory, monkeypatch):
    """A resend that actually sends returns 200; a resend still inside the
    cooldown window returns 429 rather than a false {"ok": true} (the admin
    resend path awaits the send directly instead of firing-and-forgetting,
    so it can report what really happened)."""
    import claw.api.auth as auth_module

    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_reg = await c.post("/api/auth/register", json={"email": "admin2@x.io", "password": "password123"})
        admin_token = admin_reg.json()["access_token"]
        user = await _make_imported_user(app, email="pending@x.io")

        await app.state.claw.smtp_config.set(
            provider="",
            host="smtp.example.com",
            port=587,
            username="",
            password="",
            from_address="noreply@example.com",
            use_tls=True,
            use_ssl=False,
            enabled=True,
        )

        sent: list[str] = []

        async def fake_send_email(cfg, to_address, subject, text_body, html_body=None):
            sent.append(to_address)

        monkeypatch.setattr(auth_module, "send_email", fake_send_email)

        r1 = await c.post(
            f"/api/admin/users/{user.id}/resend-activation",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r1.status_code == 200
        assert sent == ["pending@x.io"]

        # Immediately resending is inside the cooldown window — must report
        # that plainly (429), not silently succeed with no email sent.
        r2 = await c.post(
            f"/api/admin/users/{user.id}/resend-activation",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r2.status_code == 429
        assert sent == ["pending@x.io"]  # no second send
