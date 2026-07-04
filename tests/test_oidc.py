"""OIDC/social login: provider config, state, and the full code→user flow (mocked)."""

import json

import httpx
import pytest

from claw.api.auth import oidc_authenticate
from claw.api.deps import AppState
from claw.auth import oidc
from claw.auth.tokens import encode
from claw.config import Settings
from tests.conftest_app import build_api_app, client

GOOGLE_KW = dict(oidc_google_client_id="gid", oidc_google_client_secret="gsecret")


def _settings(**kw) -> Settings:
    return Settings(secret_key="s", _env_file=None, **kw)


def test_enabled_providers_requires_id_and_secret():
    assert oidc.enabled_providers(_settings()) == {}
    assert set(oidc.enabled_providers(_settings(**GOOGLE_KW))) == {"google"}
    both = _settings(**GOOGLE_KW, oidc_microsoft_client_id="m", oidc_microsoft_client_secret="ms")
    assert set(oidc.enabled_providers(both)) == {"google", "microsoft"}


def test_authorize_url_contains_expected_params():
    cfg = oidc.enabled_providers(_settings(**GOOGLE_KW))["google"]
    url = oidc.authorize_url(cfg, "https://app/cb", "state123")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=gid" in url and "state=state123" in url
    assert "redirect_uri=https%3A%2F%2Fapp%2Fcb" in url
    assert "response_type=code" in url


def test_microsoft_tenant_in_urls():
    cfg = oidc.enabled_providers(
        _settings(oidc_microsoft_client_id="m", oidc_microsoft_client_secret="s", oidc_microsoft_tenant="contoso")
    )["microsoft"]
    assert "contoso/oauth2/v2.0/authorize" in cfg.authorize_url


def test_state_sign_and_verify():
    st = oidc.make_state("google", "secret")
    assert oidc.verify_state(st, "google", "secret")
    assert not oidc.verify_state(st, "microsoft", "secret")  # provider mismatch
    assert not oidc.verify_state(st, "google", "other-secret")  # bad signature
    assert not oidc.verify_state("garbage", "google", "secret")


def test_redirect_uri_built_from_public_base():
    s = _settings(public_base_url="https://claw.example.com")
    assert oidc.redirect_uri(s, "google") == "https://claw.example.com/api/auth/oidc/google/callback"


def _mock_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_oidc_authenticate_creates_first_user_as_admin(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    state: AppState = app.state.claw

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "id_token": "x"})
        return httpx.Response(200, json={"email": "Alice@Example.com", "name": "Alice"})

    async with _mock_http(handler) as http:
        user = await oidc_authenticate(state, "google", "code123", "https://app/cb", http)

    assert user.email == "alice@example.com"  # normalized lowercase
    assert user.display_name == "Alice"
    assert user.is_admin is True  # first user


async def test_oidc_authenticate_reuses_existing_user(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    state: AppState = app.state.claw
    existing = await state.users.get_or_create_by_email("bob@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"email": "bob@example.com", "name": "Bob"})

    async with _mock_http(handler) as http:
        user = await oidc_authenticate(state, "google", "c", "https://app/cb", http)
    assert user.id == existing.id
    assert user.is_admin is False  # not the first user


async def test_identity_falls_back_to_id_token_claims(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    state: AppState = app.state.claw
    id_token = "h." + encode({"email": "claim@example.com", "name": "Claimed"}, "irrelevant", 60).split(".")[1] + ".s"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "id_token": id_token})
        return httpx.Response(401, json={})  # userinfo denied → fall back to id_token

    async with _mock_http(handler) as http:
        user = await oidc_authenticate(state, "google", "c", "https://app/cb", http)
    assert user.email == "claim@example.com"


async def test_providers_endpoint_lists_enabled(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    async with client(app) as c:
        r = await c.get("/api/auth/providers")
        assert r.status_code == 200 and r.json()["providers"] == ["google"]


async def test_login_redirects_to_provider(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    async with client(app) as c:
        r = await c.get("/api/auth/oidc/google/login")
        assert r.status_code == 307
        loc = r.headers["location"]
        assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=gid" in loc


async def test_login_unknown_provider_404(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW)
    async with client(app) as c:
        assert (await c.get("/api/auth/oidc/facebook/login")).status_code == 404


async def test_oidc_respects_closed_registration_for_new_user(db_factory):
    # Registration closed: a new social identity is rejected, but the first
    # account still bootstraps, and existing users still sign in.
    app = build_api_app(db_factory, **GOOGLE_KW, open_registration=False)
    state: AppState = app.state.claw

    def make_handler(email: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "at"})
            return httpx.Response(200, json={"email": email, "name": "X"})

        return handler

    # First user bootstraps despite closed registration.
    async with _mock_http(make_handler("first@example.com")) as http:
        first = await oidc_authenticate(state, "google", "c", "https://app/cb", http)
    assert first.is_admin is True

    # A different new identity is now rejected.
    with pytest.raises(Exception) as exc:
        async with _mock_http(make_handler("stranger@example.com")) as http:
            await oidc_authenticate(state, "google", "c", "https://app/cb", http)
    assert getattr(exc.value, "status_code", None) == 403

    # But the existing first user can still sign in.
    async with _mock_http(make_handler("first@example.com")) as http:
        again = await oidc_authenticate(state, "google", "c", "https://app/cb", http)
    assert again.id == first.id


async def test_callback_bad_state_redirects_with_error(db_factory):
    app = build_api_app(db_factory, **GOOGLE_KW, web_base_url="http://web")
    async with client(app) as c:
        r = await c.get("/api/auth/oidc/google/callback", params={"code": "c", "state": "bad"})
        assert r.status_code == 307
        assert "auth_error=invalid_state" in r.headers["location"]
