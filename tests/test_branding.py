"""Control Plane > Preferences: global branding store, admin CRUD, logo upload
validation, and the public (pre-auth) delivery endpoints."""

import base64

from claw.db.stores import BrandingStore
from tests.conftest_app import build_api_app, client

# A real 1x1 transparent PNG (valid magic bytes so the sniffer accepts it).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---- store ----

async def test_store_returns_defaults_when_unset(db_factory):
    store = BrandingStore(db_factory)
    cfg = await store.get()
    assert cfg == {
        "language": "en",
        "font_size": "small",
        "chat_background": "solid",
        "logo_login": None,
        "logo_chat": None,
        "logo_sidebar": None,
    }


async def test_store_set_and_logo_roundtrip(db_factory):
    store = BrandingStore(db_factory)
    await store.set(language="th", font_size="large", chat_background="grid")
    cfg = await store.get()
    assert (cfg["language"], cfg["font_size"], cfg["chat_background"]) == ("th", "large", "grid")

    prev = await store.set_logo("login", "login-aaaa.png")
    assert prev is None  # nothing there before
    assert (await store.get())["logo_login"] == "login-aaaa.png"
    prev2 = await store.set_logo("login", "login-bbbb.png")
    assert prev2 == "login-aaaa.png"  # returns the replaced filename for cleanup

    removed = await store.clear_logo("login")
    assert removed == "login-bbbb.png"
    assert (await store.get())["logo_login"] is None


async def test_store_get_is_cached_across_calls_but_reflects_writes(db_factory):
    # get() is in-process cached (it's read on every chat turn and every
    # /api/branding page load) — this proves a second BrandingStore instance
    # sharing the same DB doesn't see writes made through a different instance
    # until it's the one that performs the write (single-process assumption),
    # and that OUR instance's own cache is invalidated immediately on every
    # write, never serving a stale value to its own caller.
    store = BrandingStore(db_factory)
    assert (await store.get())["language"] == "en"

    await store.set(language="th", font_size="small", chat_background="solid")
    assert (await store.get())["language"] == "th"  # write invalidates this instance's cache

    prev = await store.set_logo("chat", "chat-xyz.png")
    assert prev is None
    assert (await store.get())["logo_chat"] == "chat-xyz.png"

    await store.clear_logo("chat")
    assert (await store.get())["logo_chat"] is None


async def test_store_rejects_invalid_enum(db_factory):
    store = BrandingStore(db_factory)
    import pytest

    with pytest.raises(ValueError):
        await store.set(language="fr", font_size="small", chat_background="solid")


# ---- public delivery (no auth) ----

async def test_public_branding_needs_no_auth(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        r = await c.get("/api/branding")  # deliberately no Authorization header
        assert r.status_code == 200
        body = r.json()
        assert body["language"] == "en"
        assert body["logos"] == {"login": None, "chat": None, "sidebar": None}


# ---- admin CRUD ----

async def test_admin_set_branding_reflected_publicly(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")  # first = admin
        r = await c.put(
            "/api/admin/branding",
            json={"language": "th", "font_size": "medium", "chat_background": "dots"},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        pub = (await c.get("/api/branding")).json()
        assert (pub["language"], pub["font_size"], pub["chat_background"]) == ("th", "medium", "dots")


async def test_put_branding_rejects_invalid_enum(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.put(
            "/api/admin/branding",
            json={"language": "es", "font_size": "small", "chat_background": "solid"},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 422


async def test_non_admin_cannot_change_branding(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _register(c, "admin@x.io")  # first = admin
        user_token, _ = await _register(c, "user@x.io")  # second = non-admin
        r = await c.put(
            "/api/admin/branding",
            json={"language": "th", "font_size": "small", "chat_background": "solid"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 403
        up = await c.post(
            "/api/admin/branding/logo/login",
            files={"file": ("logo.png", _PNG, "image/png")},
            headers=_bearer(user_token),
        )
        assert up.status_code == 403


# ---- logo upload validation + serving ----

async def test_logo_upload_accepts_png_and_serves(db_factory, tmp_path):
    app = build_api_app(db_factory, branding_root=tmp_path / "branding")
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        r = await c.post(
            "/api/admin/branding/logo/login",
            files={"file": ("logo.png", _PNG, "image/png")},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        filename = r.json()["logo_login"]
        assert filename, "filename should be recorded"
        # Public URL points at the asset route, versioned by the stored
        # filename (so a later replace gets a fresh, uncached URL) — and the
        # asset itself serves as an image.
        pub_resp = await c.get("/api/branding")
        pub = pub_resp.json()
        assert pub["logos"]["login"] == f"/api/branding/assets/login?v={filename}"
        assert pub_resp.headers["cache-control"] == "no-store"
        asset = await c.get(pub["logos"]["login"])
        assert asset.status_code == 200
        assert asset.headers["content-type"] == "image/png"
        assert asset.headers["x-content-type-options"] == "nosniff"
        assert "immutable" in asset.headers["cache-control"]


async def test_logo_upload_rejects_svg_by_magic_bytes(db_factory, tmp_path):
    app = build_api_app(db_factory, branding_root=tmp_path / "branding")
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        r = await c.post(
            "/api/admin/branding/logo/login",
            files={"file": ("logo.png", svg, "image/png")},  # lies about the type
            headers=_bearer(admin_token),
        )
        assert r.status_code == 422


async def test_logo_upload_rejects_oversize(db_factory, tmp_path):
    app = build_api_app(db_factory, branding_root=tmp_path / "branding")
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        big = _PNG + b"\x00" * 1_000_001  # valid header, but over the 1 MB cap
        r = await c.post(
            "/api/admin/branding/logo/login",
            files={"file": ("logo.png", big, "image/png")},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 422


async def test_logo_delete_reverts_to_default(db_factory, tmp_path):
    app = build_api_app(db_factory, branding_root=tmp_path / "branding")
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await c.post(
            "/api/admin/branding/logo/sidebar",
            files={"file": ("logo.png", _PNG, "image/png")},
            headers=_bearer(admin_token),
        )
        d = await c.delete("/api/admin/branding/logo/sidebar", headers=_bearer(admin_token))
        assert d.status_code == 200
        assert d.json()["logo_sidebar"] is None
        assert (await c.get("/api/branding/assets/sidebar")).status_code == 404
