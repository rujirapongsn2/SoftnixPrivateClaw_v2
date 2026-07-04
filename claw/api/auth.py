"""Authentication: password + OIDC (Google/Microsoft), issuing JWT bearer tokens."""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, Field

from claw.api.deps import AppState, current_user, get_state
from claw.auth import oidc
from claw.auth.passwords import hash_password, verify_password
from claw.auth.tokens import create_access_token
from claw.db.models import User

router = APIRouter(prefix="/api/auth")


_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""


class LoginBody(BaseModel):
    email: str = Field(pattern=_EMAIL_RE, max_length=255)
    password: str


def _user_json(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "role": user.role,
    }


def _issue(state: AppState, user: User) -> dict:
    token = create_access_token(user.id, state.settings.secret_key, state.settings.token_ttl_seconds)
    return {"access_token": token, "token_type": "bearer", "user": _user_json(user)}


@router.post("/register")
async def register(body: RegisterBody, state: AppState = Depends(get_state)) -> dict:
    existing = await state.users.get_by_email(body.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    # The very first account bootstraps the system administrator; afterwards
    # open registration must be enabled, otherwise only admins create users.
    total = await state.users.count()
    if total > 0 and not state.settings.open_registration:
        raise HTTPException(status_code=403, detail="registration is closed; ask an administrator")

    user = await state.users.create(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_admin=(total == 0),
        role="admin" if total == 0 else "user",
    )
    return _issue(state, user)


@router.post("/login")
async def login(body: LoginBody, state: AppState = Depends(get_state)) -> dict:
    user = await state.users.get_by_email(body.email)
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account suspended")
    return _issue(state, user)


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return _user_json(user)


# ---------------------------------------------------------------- OIDC / social login

async def _upsert_oidc_user(state: AppState, email: str, name: str) -> User:
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
    return await state.users.create(
        email=email,
        display_name=name or email.split("@")[0],
        is_admin=(total == 0),
        role="admin" if total == 0 else "user",
    )


async def oidc_authenticate(
    state: AppState, provider: str, code: str, redirect: str, http: httpx.AsyncClient
) -> User:
    """Exchange an auth code for identity and resolve it to an active Claw user."""
    cfg = oidc.enabled_providers(state.settings).get(provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail="provider not configured")
    tokens = await oidc.exchange_code(cfg, code, redirect, http)
    email, name = await oidc.fetch_identity(cfg, tokens, http)
    if not email:
        raise HTTPException(status_code=400, detail="provider did not return an email")
    user = await _upsert_oidc_user(state, email, name)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account suspended")
    return user


@router.get("/providers")
async def list_providers(state: AppState = Depends(get_state)) -> dict:
    return {"providers": list(oidc.enabled_providers(state.settings).keys())}


@router.get("/oidc/{provider}/login")
async def oidc_login(provider: str, state: AppState = Depends(get_state)) -> RedirectResponse:
    cfg = oidc.enabled_providers(state.settings).get(provider)
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

    jwt = create_access_token(user.id, app_state.settings.secret_key, app_state.settings.token_ttl_seconds)
    return RedirectResponse(f"{web}/?token={jwt}", status_code=307)
