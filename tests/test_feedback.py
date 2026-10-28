from claw.db.stores import FeedbackStore
from tests.conftest_app import build_api_app, client


async def test_store_records_and_aggregates(db_factory):
    fb = FeedbackStore(db_factory)
    await fb.record("u1", "s1", "up", "great", "the reply", "k1")
    await fb.record("u1", "s1", "up", "", "another", "k2")
    await fb.record("u1", "s2", "down", "wrong", "bad reply", "k3")
    await fb.record("u2", "s3", "up", "", "x", "k4")

    assert await fb.totals_for_user("u1") == {"up": 2, "down": 1}
    assert await fb.totals() == {"up": 3, "down": 1}


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def _session(c, token):
    s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))
    return s.json()["id"]


def _body(sid, signal, key="5:abc"):
    return {"signal": signal, "session_id": sid, "message_key": key, "message_preview": "hi"}


async def test_rerating_updates_instead_of_adding(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        sid = await _session(c, token)
        for signal in ("up", "up", "down"):
            r = await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, signal))
            assert r.status_code == 200 and r.json()["recorded"] is True
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "up", "9:zzz"))

        stats = await c.get("/api/feedback/stats", headers=_bearer(token))
        assert stats.json() == {"up": 1, "down": 1}
        saved = await c.get(f"/api/sessions/{sid}/feedback", headers=_bearer(token))
        assert saved.json() == {"5:abc": "down", "9:zzz": "up"}


async def test_none_clears_the_rating(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        sid = await _session(c, token)
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "up"))
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "none"))
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "none", "1:never"))
        stats = await c.get("/api/feedback/stats", headers=_bearer(token))
        assert stats.json() == {"up": 0, "down": 0}
        saved = await c.get(f"/api/sessions/{sid}/feedback", headers=_bearer(token))
        assert saved.json() == {}


async def test_feedback_requires_session_and_key(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        sid = await _session(c, token)
        for body in ({"signal": "up"}, {"signal": "up", "session_id": sid}, _body(sid, "up", "bad key!")):
            r = await c.post("/api/feedback", headers=_bearer(token), json=body)
            assert r.status_code == 422


async def test_invalid_signal_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        sid = await _session(c, token)
        r = await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "sideways"))
        assert r.status_code == 422


async def test_feedback_on_foreign_session_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token_a, _ = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        sid = await _session(c, token_a)
        r = await c.post("/api/feedback", headers=_bearer(token_b), json=_body(sid, "up"))
        assert r.status_code == 404
        r = await c.get(f"/api/sessions/{sid}/feedback", headers=_bearer(token_b))
        assert r.status_code == 404


async def test_admin_stats_include_feedback(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)  # first user = admin
        sid = await _session(c, token)
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "up", "1:a"))
        await c.post("/api/feedback", headers=_bearer(token), json=_body(sid, "up", "1:b"))
        stats = await c.get("/api/admin/stats", headers=_bearer(token))
        assert stats.json()["feedback_up"] == 2 and stats.json()["feedback_down"] == 0
