from claw.db.stores import FeedbackStore
from tests.conftest_app import build_api_app, client


async def test_store_records_and_aggregates(db_factory):
    fb = FeedbackStore(db_factory)
    await fb.record("u1", "s1", "up", "great", "the reply")
    await fb.record("u1", "s1", "up", "", "another")
    await fb.record("u1", "s2", "down", "wrong", "bad reply")
    await fb.record("u2", "s3", "up", "", "x")

    assert await fb.totals_for_user("u1") == {"up": 2, "down": 1}
    assert await fb.totals() == {"up": 3, "down": 1}


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def test_feedback_endpoint_records_and_stats(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))
        sid = s.json()["id"]

        r = await c.post(
            "/api/feedback",
            headers=_bearer(token),
            json={"signal": "up", "session_id": sid, "message_preview": "hi there"},
        )
        assert r.status_code == 200 and r.json()["recorded"] is True

        await c.post("/api/feedback", headers=_bearer(token), json={"signal": "down", "note": "meh"})

        stats = await c.get("/api/feedback/stats", headers=_bearer(token))
        assert stats.json() == {"up": 1, "down": 1}


async def test_invalid_signal_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)
        r = await c.post("/api/feedback", headers=_bearer(token), json={"signal": "sideways"})
        assert r.status_code == 422


async def test_feedback_on_foreign_session_rejected(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token_a, _ = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        s = await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token_a))
        sid = s.json()["id"]
        r = await c.post(
            "/api/feedback", headers=_bearer(token_b), json={"signal": "up", "session_id": sid}
        )
        assert r.status_code == 404


async def test_admin_stats_include_feedback(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c)  # first user = admin
        await c.post("/api/feedback", headers=_bearer(token), json={"signal": "up"})
        await c.post("/api/feedback", headers=_bearer(token), json={"signal": "up"})
        stats = await c.get("/api/admin/stats", headers=_bearer(token))
        assert stats.json()["feedback_up"] == 2 and stats.json()["feedback_down"] == 0
