"""Admin multi-tenant API."""

from tests.conftest_app import build_api_app, client


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_admin_lists_and_creates_users(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await _register(c, "u1@x.io")

        r = await c.get("/api/admin/users", headers=_bearer(admin_token))
        assert r.status_code == 200
        assert {u["email"] for u in r.json()} == {"admin@x.io", "u1@x.io"}

        created = await c.post(
            "/api/admin/users",
            json={"email": "u2@x.io", "password": "password123", "is_admin": True},
            headers=_bearer(admin_token),
        )
        assert created.status_code == 200 and created.json()["is_admin"] is True


async def test_import_users_parse_reads_a_csv(db_factory):
    # Regression coverage for the CSV branch, which runs in a worker thread
    # rather than on the event loop — must still return the parsed grid unchanged.
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")

        r = await c.post(
            "/api/admin/users/import/parse",
            files={"file": ("users.csv", b"email,name\nu1@x.io,User One\nu2@x.io,User Two\n", "text/csv")},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["columns"] == ["email", "name"]
        assert body["rows"] == [["u1@x.io", "User One"], ["u2@x.io", "User Two"]]


async def test_import_users_parse_keeps_regional_names_intact(db_factory):
    """The importer decodes through file_preview.decode_text_bytes, which tries
    cp874 before cp1252 — the reverse of the encoding list this endpoint used to
    carry. That order is what stops a Thai export being silently mangled (cp1252
    decodes TIS-620 bytes without error, so a first-clean-decode-wins rule always
    picked it), and the Thai check is what stops it running the other way. Names
    are the content this endpoint actually receives, so they are what it is
    pinned against.

    Known boundary: a field of *only* consecutive uppercase accented letters
    (ÀÁÂÃ) does decode as Thai, because those bytes are Thai consonants in cp874
    and nothing else in the field breaks the syllable shape. Real names interleave
    ASCII letters, which is why the cases below survive; a column of bare accented
    initials would not."""
    app = build_api_app(db_factory)
    cases = {
        "french.csv": ("cp1252", "email,name\na@x.io,José Périgord\nb@x.io,René Lefèvre\n"),
        "nordic.csv": ("cp1252", "email,name\na@x.io,Øyvind Kårstad\nb@x.io,Åsa Lindström\n"),
        "german.csv": ("cp1252", "email,name\na@x.io,Jürgen Groß\nb@x.io,Björn Öberg\n"),
        "thai.csv": ("cp874", "email,name\na@x.io,สมชาย ใจดี\nb@x.io,สุนิสา ทองมา\n"),
    }
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        for filename, (encoding, text) in cases.items():
            r = await c.post(
                "/api/admin/users/import/parse",
                files={"file": (filename, text.encode(encoding), "text/csv")},
                headers=_bearer(admin_token),
            )
            assert r.status_code == 200, filename
            expected = [line.split(",") for line in text.strip().splitlines()]
            assert r.json()["rows"] == expected[1:], filename


async def test_non_admin_denied(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "normal@x.io")
        assert (await c.get("/api/admin/users", headers=_bearer(user_token))).status_code == 403


async def test_admin_suspends_user_who_then_cannot_act(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        victim_token, victim = await _register(c, "victim@x.io")

        # Victim works before suspension.
        assert (await c.get("/api/skills", headers=_bearer(victim_token))).status_code == 200

        r = await c.patch(
            f"/api/admin/users/{victim['id']}",
            json={"is_active": False},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200 and r.json()["is_active"] is False

        # Suspended → 403 even with a still-valid token.
        assert (await c.get("/api/skills", headers=_bearer(victim_token))).status_code == 403


async def test_admin_promotes_user(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "normal@x.io")

        await c.patch(f"/api/admin/users/{user['id']}", json={"is_admin": True}, headers=_bearer(admin_token))
        # Now the promoted user can reach admin endpoints.
        assert (await c.get("/api/admin/users", headers=_bearer(user_token))).status_code == 200


async def test_admin_cannot_demote_self(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, admin = await _register(c, "admin@x.io")
        r = await c.patch(
            f"/api/admin/users/{admin['id']}", json={"is_admin": False}, headers=_bearer(admin_token)
        )
        assert r.status_code == 400


async def test_stats(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        await _register(c, "u1@x.io")
        r = await c.get("/api/admin/stats", headers=_bearer(admin_token))
        assert r.status_code == 200
        body = r.json()
        assert body["users"] == 2 and body["admins"] == 1
