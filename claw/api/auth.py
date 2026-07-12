"""Authentication: password + OIDC (Google/Microsoft), issuing JWT bearer tokens."""

import asyncio
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, Field

from claw.api.deps import AppState, current_user, get_state
from claw.auth import oidc
from claw.auth.activation import make_activation_token, verify_activation_token
from claw.auth.password_reset import make_password_reset_token, verify_password_reset_token
from claw.auth.passwords import hash_password, verify_password
from claw.auth.tokens import create_access_token
from claw.db.models import User
from claw.notifications.mailer import send_email

router = APIRouter(prefix="/api/auth")


_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""


class LoginBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str


class CompleteRegistrationBody(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = None


class ActivationInfoBody(BaseModel):
    token: str


class ForgotPasswordBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)


class ResetPasswordBody(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


def _user_json(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "role": user.role,
        # False for an OAuth-only or not-yet-activated account — the
        # frontend uses this to decide whether to show a "change password"
        # form at all (there's no password to change otherwise).
        "has_password": bool(user.password_hash),
    }


def _issue(state: AppState, user: User) -> dict:
    token = create_access_token(user.id, state.settings.secret_key, state.settings.token_ttl_seconds)
    return {"access_token": token, "token_type": "bearer", "user": _user_json(user)}


async def _log_auth(state: AppState, event: str, user: User, method: str = "password") -> None:
    """Audit trail for account activity — surfaced in the Control Plane's audit
    log (kind="auth") so admins can see who signed in/out, and how."""
    await state.audit.log(
        "auth",
        {"event": event, "method": method, "email": user.email},
        user_id=user.id,
    )


# --------------------------------------------------- imported-user activation email

_bg_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    """Fire-and-forget a coroutine, keeping a strong reference until it
    finishes so it can't be garbage-collected mid-flight (asyncio only holds
    a weak reference to a task otherwise). The coroutine must handle its own
    errors — nothing here observes or re-raises them."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _send_activation_email(state: AppState, user_id: str) -> str:
    """Best-effort: send (or resend, subject to a cooldown) the imported-user
    activation link. Returns "sent" | "cooldown" | "disabled" |
    "not_applicable" | "failed" so a synchronous caller (the admin resend
    endpoint) can report the true outcome instead of always claiming success.

    Must never raise, and must do ALL of its work — including the cooldown
    claim — after being scheduled, not before: login()/register() call this
    via _spawn_background() and ignore the return value, specifically so its
    latency (a DB read/write plus an SMTP round trip) is never observed by
    the caller. Checking the cooldown synchronously before scheduling would
    reopen, as a timing side-channel, the exact account-enumeration oracle
    this flow exists to close.
    """
    try:
        smtp = await state.smtp_config.get()
        if not smtp or not smtp.get("enabled"):
            return "disabled"
        user = await state.users.get(user_id)
        if user is None or user.signup_method != "imported" or user.password_hash or not user.is_active:
            return "not_applicable"
        now = datetime.now(timezone.utc)
        # Atomically claim the send slot (a conditional UPDATE) BEFORE
        # sending — a read-then-write cooldown check lets concurrent
        # requests all observe the same stale timestamp and all send,
        # turning the cooldown into a no-op under concurrent triggering
        # (e.g. an attacker firing repeated /login attempts for a known
        # imported email to mail-bomb the real owner's inbox).
        claimed = await state.users.claim_activation_send(
            user.id, now, state.settings.activation_email_resend_cooldown_seconds
        )
        if not claimed:
            return "cooldown"
        token = make_activation_token(
            user.id, state.settings.secret_key, state.settings.activation_token_ttl_seconds
        )
        # A URL fragment (#), not a query string (?) — fragments are never
        # sent to the server, so neither the web frontend's nor any
        # reverse-proxy/CDN's access logs ever see this login-granting
        # token in the initial page-load request.
        link = f"{state.settings.web_base_url.rstrip('/')}/#activate={token}"
        await send_email(
            smtp,
            user.email,
            subject="Activate your PrivateClaw account",
            text_body=(
                f"Hi {user.display_name or user.email},\n\n"
                "An administrator has added you to PrivateClaw. Click the link below "
                f"to set your password and finish activating your account:\n\n{link}\n\n"
                "This link expires in a few hours. If you weren't expecting this, "
                "you can safely ignore it."
            ),
        )
        await state.audit.log(
            "auth", {"event": "activation_email_sent", "email": user.email}, user_id=user.id
        )
        return "sent"
    except Exception:
        logger.warning("activation email send failed for user {}", user_id, exc_info=True)
        return "failed"


async def _send_activation_confirmed_email(state: AppState, user_id: str) -> None:
    """Best-effort "your account is now active" notice — lets the real owner
    notice (and report it) if an attacker somehow intercepted and redeemed
    the activation link first. Fire-and-forget only to keep SMTP latency off
    the completing user's login response; unlike _send_activation_email
    there's no cooldown/enumeration concern here (the caller just proved
    token possession), so failures are simply logged."""
    try:
        smtp = await state.smtp_config.get()
        if not smtp or not smtp.get("enabled"):
            return
        user = await state.users.get(user_id)
        if user is None:
            return
        await send_email(
            smtp,
            user.email,
            subject="Your PrivateClaw account is now active",
            text_body=(
                f"Hi {user.display_name or user.email},\n\n"
                "Your PrivateClaw account was just activated. If this wasn't "
                "you, contact your administrator immediately."
            ),
        )
    except Exception:
        logger.warning("activation-confirmed email failed for user {}", user_id, exc_info=True)


async def _send_password_reset_email(state: AppState, user_id: str) -> str:
    """Best-effort: send (subject to a cooldown) a "forgot password" reset
    link. Returns "sent" | "cooldown" | "disabled" | "not_applicable" |
    "failed" — mirrors _send_activation_email's contract exactly, including
    doing ALL of its work (the cooldown claim included) after being
    scheduled: forgot_password() fires this via _spawn_background() and
    ignores the return value, so an existing account and a nonexistent one
    produce identical response latency (no enumeration timing oracle)."""
    try:
        smtp = await state.smtp_config.get()
        if not smtp or not smtp.get("enabled"):
            return "disabled"
        user = await state.users.get(user_id)
        if user is None or not user.password_hash or not user.is_active:
            return "not_applicable"
        now = datetime.now(timezone.utc)
        nonce = secrets.token_urlsafe(8)
        # Atomically claim the cooldown AND record the nonce that will be
        # embedded in the emailed token, so redeem_password_reset() can
        # later enforce single-use via compare-and-swap (see that method's
        # docstring — this closes the TOCTOU gap the activation flow's
        # equivalent has to accept as low-impact debt, since here we're
        # building fresh and it costs nothing extra).
        claimed = await state.users.claim_password_reset_send(
            user.id, now, state.settings.password_reset_resend_cooldown_seconds, nonce
        )
        if not claimed:
            return "cooldown"
        token = make_password_reset_token(
            user.id, nonce, state.settings.secret_key, state.settings.password_reset_token_ttl_seconds
        )
        # URL fragment, not a query string — never sent to the server, so
        # this password-setting token never lands in an access log.
        link = f"{state.settings.web_base_url.rstrip('/')}/#reset-password={token}"
        await send_email(
            smtp,
            user.email,
            subject="Reset your PrivateClaw password",
            text_body=(
                f"Hi {user.display_name or user.email},\n\n"
                "We received a request to reset your PrivateClaw password. Click "
                f"the link below to choose a new one:\n\n{link}\n\n"
                "This link expires in an hour. If you didn't request this, you can "
                "safely ignore it — your password won't change."
            ),
        )
        await state.audit.log(
            "auth", {"event": "password_reset_email_sent", "email": user.email}, user_id=user.id
        )
        return "sent"
    except Exception:
        logger.warning("password reset email send failed for user {}", user_id, exc_info=True)
        return "failed"


async def _send_password_reset_confirmed_email(state: AppState, user_id: str) -> None:
    """Best-effort "your password was just changed" notice — lets the real
    owner notice (and report it) if someone else intercepted and redeemed
    the reset link first. Detached for the same reason as the activation
    confirmation email: never let SMTP latency delay the completing user's
    login response."""
    try:
        smtp = await state.smtp_config.get()
        if not smtp or not smtp.get("enabled"):
            return
        user = await state.users.get(user_id)
        if user is None:
            return
        await send_email(
            smtp,
            user.email,
            subject="Your PrivateClaw password was changed",
            text_body=(
                f"Hi {user.display_name or user.email},\n\n"
                "Your PrivateClaw password was just changed. If this wasn't "
                "you, contact your administrator immediately."
            ),
        )
    except Exception:
        logger.warning("password-reset-confirmed email failed for user {}", user_id, exc_info=True)


@router.post("/register")
async def register(body: RegisterBody, state: AppState = Depends(get_state)) -> dict:
    existing = await state.users.get_by_email(body.email)
    if existing is not None:
        # A bulk-imported, not-yet-activated row. Do NOT reveal that
        # distinction to the caller (that's the account-enumeration oracle
        # this used to be) — same generic 409 as any other duplicate email.
        # The only difference is a side effect: (re)send the activation link
        # to the real address, detached so it can't be timed either.
        if existing.signup_method == "imported" and not existing.password_hash:
            _spawn_background(_send_activation_email(state, existing.id))
        raise HTTPException(status_code=409, detail="email already registered")

    # The very first account bootstraps the system administrator; afterwards
    # open registration must be enabled, otherwise only admins create users.
    total = await state.users.count()
    if total > 0 and not state.settings.open_registration:
        raise HTTPException(status_code=403, detail="registration is closed; ask an administrator")

    # Self-registered users land in the admin-configured default group (if any).
    default_group = await state.groups.default_group()
    user = await state.users.create(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=(total == 0),
        role="admin" if total == 0 else "user",
        group_id=default_group.id if default_group else None,
        signup_method="password",
    )
    await _log_auth(state, "register", user)
    return _issue(state, user)


@router.post("/login")
async def login(body: LoginBody, state: AppState = Depends(get_state)) -> dict:
    user = await state.users.get_by_email(body.email)
    # A bulk-imported user has no password yet (password_hash == "") — but so
    # can an OIDC-only account, which must keep failing here exactly as
    # before. Gate tightly on signup_method == "imported" so this branch only
    # ever fires for rows created by the CSV/XLSX import, never lets someone
    # "claim" a Google/Microsoft-only account by setting a password for it.
    # Deliberately does NOT return anything different to the caller for this
    # case — `not user.password_hash` below already fails the exact same way
    # a wrong password or no-such-user does. The only effect is a detached
    # side effect (an activation email), which must never change this
    # endpoint's response status, body, or observable latency.
    if user is not None and user.signup_method == "imported" and not user.password_hash:
        _spawn_background(_send_activation_email(state, user.id))
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account suspended")
    await _log_auth(state, "login", user)
    return _issue(state, user)


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordBody, state: AppState = Depends(get_state)) -> dict:
    """Always returns the same generic response whether or not the email
    matches an account with a password — revealing that distinction would be
    an account-enumeration oracle (the same class of bug closed on the
    imported-user activation flow). If it does match a real, active,
    password-set account, a reset email is sent as a detached side effect
    (see _send_password_reset_email) so this response's latency never
    differs either."""
    user = await state.users.get_by_email(body.email)
    if user is not None and user.password_hash and user.is_active:
        _spawn_background(_send_password_reset_email(state, user.id))
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordBody, state: AppState = Depends(get_state)) -> dict:
    """Redeems a signed password-reset link (claw/auth/password_reset.py).
    The token's nonce is the proof of identity, matched against
    users.password_reset_nonce via an atomic compare-and-swap
    (UserStore.redeem_password_reset) — so the same token can't be redeemed
    twice, and requesting a new reset invalidates any prior unredeemed one.
    That same UPDATE also gates on the account being active, so a suspended
    account's password is never written — not merely rejected after the
    fact, which would leave the DB mutated even though the caller sees a
    403 (this is why _log_auth/the confirmation email below are reachable
    ONLY when the write itself actually happened)."""
    parsed = verify_password_reset_token(body.token, state.settings.secret_key)
    if parsed is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset link")
    uid, nonce = parsed
    redeemed = await state.users.redeem_password_reset(uid, nonce, hash_password(body.password))
    if not redeemed:
        # The atomic UPDATE can fail for two different reasons (bad/reused
        # nonce vs. a genuinely-active token on a suspended account) and the
        # caller deserves an accurate reason — this follow-up read is safe
        # precisely because reaching this line already required a
        # cryptographically valid, correctly-signed token for this uid (not
        # forgeable without the HMAC secret), so it isn't a new
        # account-enumeration surface for a third party without the token.
        user = await state.users.get(uid)
        if user is not None and not user.is_active:
            raise HTTPException(status_code=403, detail="account suspended")
        raise HTTPException(status_code=400, detail="invalid or expired reset link")
    updated = await state.users.get(uid)
    if updated is None:
        raise HTTPException(status_code=404, detail="account no longer exists")
    await _log_auth(state, "password_reset", updated)
    _spawn_background(_send_password_reset_confirmed_email(state, updated.id))
    if not updated.is_active:
        # Extremely rare race: suspended in the instant between the
        # password write above (which only succeeds while active) and this
        # re-fetch. The write was legitimate and is already logged/notified
        # — just don't hand back a session for a now-suspended account.
        raise HTTPException(status_code=403, detail="account suspended")
    return _issue(state, updated)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    """Self-service password change for an already-authenticated user.
    Unlike reset-password (which proves identity via an emailed token, for
    someone who's locked out), this proves identity by requiring the
    CURRENT password — appropriate since the caller is already holding a
    valid session."""
    if not user.password_hash:
        raise HTTPException(
            status_code=400,
            detail="This account doesn't have a password to change (signed in via Google/Microsoft, "
            "or hasn't finished activation yet).",
        )
    if not verify_password(body.current_password, user.password_hash):
        # 403, not 401: the caller IS authenticated (a valid bearer token got
        # them past `current_user` above) — this codebase's frontend treats
        # ANY 401 response as "the session token itself is invalid" and
        # force-logs-out (clearToken() in web/src/api.ts's request()). Using
        # 401 here would silently log a user out just for mistyping their
        # current password.
        raise HTTPException(status_code=403, detail="current password is incorrect")
    await state.users.update_profile(user.id, password_hash=hash_password(body.new_password))
    await _log_auth(state, "password_changed", user)
    _spawn_background(_send_password_reset_confirmed_email(state, user.id))
    return {"ok": True}


@router.post("/activation")
async def activation_info(body: ActivationInfoBody, state: AppState = Depends(get_state)) -> dict:
    """Decode an emailed activation link so the frontend can prefill the
    set-password form. POST with the token in the body (not a GET with the
    token in the URL path) so this lookup never puts the token in a
    reverse-proxy/CDN/server access log. Safe to expose without auth: a
    valid token can't be forged without the HMAC secret key, so this doesn't
    reopen account enumeration — any invalid/expired/stale token gets the
    same generic 400 as any other, never a distinguishing reason."""
    uid = verify_activation_token(body.token, state.settings.secret_key)
    user = await state.users.get(uid) if uid else None
    if user is None or user.signup_method != "imported" or user.password_hash:
        raise HTTPException(status_code=400, detail="invalid or expired activation link")
    return {"email": user.email, "display_name": user.display_name}


@router.post("/complete-registration")
async def complete_registration(body: CompleteRegistrationBody, state: AppState = Depends(get_state)) -> dict:
    """Redeems a signed activation link (claw/auth/activation.py) to let a
    bulk-imported user (signup_method="imported", no password yet) set their
    own password and get logged in immediately. The token — not a bare email
    — is the proof of identity: it's only ever delivered by emailing it to
    the address an admin imported, so redeeming it proves inbox ownership
    instead of merely knowing/guessing the address."""
    uid = verify_activation_token(body.token, state.settings.secret_key)
    user = await state.users.get(uid) if uid else None
    if user is None or user.signup_method != "imported" or user.password_hash:
        raise HTTPException(status_code=400, detail="invalid or expired activation link")
    if not user.is_active:
        # Suspended between import and first login — must not be able to
        # self-activate around that suspension, same as login()'s check.
        raise HTTPException(status_code=403, detail="account suspended")
    await state.users.update_profile(
        user.id, display_name=body.display_name or None, password_hash=hash_password(body.password)
    )
    updated = await state.users.get(user.id)
    if updated is None:
        # Extremely rare: the row vanished between the write above and this
        # refetch (e.g. a concurrent admin delete) — the password write still
        # happened, but there's no user left to issue a session for.
        raise HTTPException(status_code=404, detail="account no longer exists")
    await _log_auth(state, "complete_registration", updated)
    # Best-effort confirmation email — lets the real owner notice (and report
    # it) if an attacker somehow intercepted and redeemed the link first.
    # Detached (like the activation send) so a slow/unreachable SMTP server
    # never delays the login response the user is waiting on.
    _spawn_background(_send_activation_confirmed_email(state, updated.id))
    return _issue(state, updated)


@router.post("/logout")
async def logout(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    # JWTs are stateless — there's nothing to invalidate server-side. This
    # endpoint exists purely so the audit trail records the logout, which is
    # why the frontend must call it BEFORE discarding the token locally.
    await _log_auth(state, "logout", user)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return _user_json(user)


# ---------------------------------------------------------------- OIDC / social login

async def _resolve_provider(state: AppState, provider: str):
    """Build a social-login config for `provider`, preferring the admin-registered
    OAuth app in the DB and falling back to environment settings. This is what
    lets one console-configured Google/Microsoft app power both login and
    connectors. Returns None when neither source has usable credentials."""
    creds = await state.oauth_apps.get(provider)
    s = state.settings
    if provider == "google":
        client_id = creds.get("client_id") or s.oidc_google_client_id
        client_secret = creds.get("client_secret") or s.oidc_google_client_secret
        tenant = ""
    elif provider == "microsoft":
        client_id = creds.get("client_id") or s.oidc_microsoft_client_id
        client_secret = creds.get("client_secret") or s.oidc_microsoft_client_secret
        tenant = creds.get("tenant") or s.oidc_microsoft_tenant
    else:
        return None
    if not (client_id and client_secret):
        return None
    return oidc.provider_config(
        provider, client_id=client_id, client_secret=client_secret, tenant=tenant
    )


async def _resolve_providers(state: AppState) -> dict:
    out = {}
    for provider in ("google", "microsoft"):
        cfg = await _resolve_provider(state, provider)
        if cfg is not None:
            out[provider] = cfg
    return out


async def _upsert_oidc_user(state: AppState, provider: str, email: str, name: str) -> User:
    """Find or create a Claw user for an OIDC identity.

    Registration rules mirror password sign-up: the first account bootstraps the
    admin; creating further accounts requires open_registration. Existing users
    always sign in regardless of that flag.
    """
    user = await state.users.get_by_email(email)
    if user is not None:
        return user
    total = await state.users.count()
    if total > 0 and not state.settings.open_registration:
        raise HTTPException(status_code=403, detail="registration is closed; ask an administrator")
    default_group = await state.groups.default_group()
    return await state.users.create(
        email=email,
        display_name=name or email.split("@")[0],
        is_admin=(total == 0),
        role="admin" if total == 0 else "user",
        group_id=default_group.id if default_group else None,
        signup_method=provider,
    )


async def oidc_authenticate(
    state: AppState, provider: str, code: str, redirect: str, http: httpx.AsyncClient
) -> User:
    """Exchange an auth code for identity and resolve it to an active Claw user."""
    cfg = await _resolve_provider(state, provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not configured")
    tokens = await oidc.exchange_code(cfg, code, redirect, http)
    email, name = await oidc.fetch_identity(cfg, tokens, http)
    if not email:
        raise HTTPException(status_code=400, detail="provider did not return an email")
    user = await _upsert_oidc_user(state, provider, email, name)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account suspended")
    return user


@router.get("/providers")
async def list_providers(state: AppState = Depends(get_state)) -> dict:
    return {"providers": list((await _resolve_providers(state)).keys())}


@router.get("/oidc/{provider}/login")
async def oidc_login(provider: str, state: AppState = Depends(get_state)) -> RedirectResponse:
    cfg = await _resolve_provider(state, provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not configured")
    redirect = oidc.redirect_uri(state.settings, provider)
    token = oidc.make_state(provider, state.settings.secret_key)
    return RedirectResponse(oidc.authorize_url(cfg, redirect, token), status_code=307)


@router.get("/oidc/{provider}/callback")
async def oidc_callback(
    provider: str,
    code: str = "",
    state: str = "",
    app_state: AppState = Depends(get_state),
) -> RedirectResponse:
    web = app_state.settings.web_base_url.rstrip("/")
    if not code or not oidc.verify_state(state, provider, app_state.settings.secret_key):
        return RedirectResponse(f"{web}/?auth_error=invalid_state", status_code=307)
    redirect = oidc.redirect_uri(app_state.settings, provider)
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            user = await oidc_authenticate(app_state, provider, code, redirect, http)
    except HTTPException as exc:
        return RedirectResponse(f"{web}/?auth_error={exc.status_code}", status_code=307)
    except Exception as exc:
        logger.warning("OIDC callback failed for {}: {}", provider, exc)
        return RedirectResponse(f"{web}/?auth_error=exchange_failed", status_code=307)

    await _log_auth(app_state, "login", user, method=provider)
    jwt = create_access_token(user.id, app_state.settings.secret_key, app_state.settings.token_ttl_seconds)
    return RedirectResponse(f"{web}/?token={jwt}", status_code=307)
