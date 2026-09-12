"""Pinning a chat keeps it above the date-grouped sidebar sections regardless
of updated_at, without the pin itself counting as an update to the chat."""

from tests.conftest_app import build_api_app, client


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def test_pinning_a_session_marks_it_pinned_in_the_list(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))
        sid = s.json()["id"]

        listed = await c.get("/api/sessions", headers=_bearer(token))
        assert listed.json()[0]["pinned"] is False

        pin = await c.post(f"/api/sessions/{sid}/pin", headers=_bearer(token))
        assert pin.status_code == 200 and pin.json() == {"pinned": True}

        listed = await c.get("/api/sessions", headers=_bearer(token))
        assert listed.json()[0]["pinned"] is True


async def test_unpinning_reverts_it(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))
        sid = s.json()["id"]

        await c.post(f"/api/sessions/{sid}/pin", headers=_bearer(token))
        unpin = await c.delete(f"/api/sessions/{sid}/pin", headers=_bearer(token))
        assert unpin.status_code == 200 and unpin.json() == {"pinned": False}

        listed = await c.get("/api/sessions", headers=_bearer(token))
        assert listed.json()[0]["pinned"] is False


async def test_pinning_a_foreign_session_is_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token_a, _ = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token_a))
        sid = s.json()["id"]

        r = await c.post(f"/api/sessions/{sid}/pin", headers=_bearer(token_b))
        assert r.status_code == 404


async def test_pinning_does_not_bump_updated_at(db_factory):
    # Pinning must not look like activity: a stale chat pinned for reference
    # should not jump to "Today" the way any real edit would.
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))
        sid = s.json()["id"]
        before = (await c.get("/api/sessions", headers=_bearer(token))).json()[0]["updated_at"]

        await c.post(f"/api/sessions/{sid}/pin", headers=_bearer(token))

        after = (await c.get("/api/sessions", headers=_bearer(token))).json()[0]["updated_at"]
        assert after == before
