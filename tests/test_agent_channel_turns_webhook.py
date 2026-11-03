"""Agent Channel: listing a conversation's turns (a caller that lost the turn
id can still find its result) and the signed completion webhook."""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import claw.channels.agent as agent_module
from claw.channels import agent_webhook
from claw.channels.agent import AgentChannelService
from claw.db.models import AgentClient, AgentTurn
from tests.conftest_app import build_api_app, client
from tests.test_agent_channel import _FakeRuntime, _service


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from claw.api import agent_channel as api_module

    api_module._failed_auth._windows.clear()
    api_module._per_client._windows.clear()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _register(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"]


async def _app_with_keys(db_factory, runtime=None):
    app = build_api_app(db_factory)
    state = app.state.claw
    state.agent_channel = AgentChannelService(
        runtime or _FakeRuntime(reply="pong"), state.agent_channel_store, state.sessions, state.users,
        state.connectors, state.audit,
    )
    return app, state


# ---------------------------------------------------------------- listing

async def test_a_lost_turn_id_is_found_by_listing_its_conversation(db_factory, monkeypatch):
    monkeypatch.setattr(agent_module, "WAIT_SECONDS", 0.05)
    app, state = await _app_with_keys(db_factory, _FakeRuntime(reply="the report", delay=0.3))
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        key = (await c.post("/api/agent-clients", json={"name": "G"}, headers=_bearer(user))).json()["key"]

        first = await c.post("/api/agent/v1/messages", json={"message": "build the   weekly\\nreport", "conversation_id": "tesla"},
                             headers=_bearer(key))
        assert first.status_code == 202  # the caller hangs up here and forgets the turn id

        working = (await c.get("/api/agent/v1/conversations/tesla/turns", headers=_bearer(key))).json()["turns"]
        assert [t["status"] for t in working] == ["working"] and working[0]["reply"] is None
        assert working[0]["message_preview"] == "build the weekly\\nreport"
        await asyncio.sleep(0.4)
        done = (await c.get("/api/agent/v1/conversations/tesla/turns?limit=1", headers=_bearer(key))).json()["turns"]
        assert done[0]["turn_id"] == first.json()["turn_id"]
        assert (done[0]["status"], done[0]["reply"]) == ("completed", "the report")
        assert done[0]["finished_at"] and done[0]["webhook"] is None


async def test_listing_is_newest_first_filtered_and_scoped_to_the_key(db_factory):
    app, state = await _app_with_keys(db_factory)
    store = state.agent_channel_store
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        other = await _register(c, "other@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        mine = (await c.post("/api/agent-clients", json={"name": "A"}, headers=_bearer(user))).json()
        second = (await c.post("/api/agent-clients", json={"name": "B"}, headers=_bearer(user))).json()
        theirs = (await c.post("/api/agent-clients", json={"name": "C"}, headers=_bearer(other))).json()

        base = datetime.now(timezone.utc) - timedelta(hours=1)
        ids = []
        for i, (cid, conv) in enumerate([(mine, "c1"), (mine, "c1"), (mine, "c2"), (second, "c1"), (theirs, "c1")]):
            turn, _ = await store.start_turn(cid["client"]["id"], conv, None, f"m{i}")
            await store.finish_turn(turn.id, "failed" if i == 1 else "completed", f"r{i}")
            async with db_factory() as db:
                (await db.get(AgentTurn, turn.id)).created_at = base + timedelta(minutes=i)
                await db.commit()
            ids.append(turn.id)

        k = _bearer(mine["key"])
        listed = (await c.get("/api/agent/v1/conversations/c1/turns", headers=k)).json()["turns"]
        assert [t["turn_id"] for t in listed] == [ids[1], ids[0]]  # newest first, own key and conversation only
        failed = (await c.get("/api/agent/v1/conversations/c1/turns?status=failed", headers=k)).json()["turns"]
        assert [t["turn_id"] for t in failed] == [ids[1]]
        everything = (await c.get("/api/agent/v1/turns", headers=k)).json()["turns"]
        assert [t["turn_id"] for t in everything] == [ids[2], ids[1], ids[0]]
        since = (base + timedelta(minutes=1, seconds=30)).isoformat()
        recent = (await c.get("/api/agent/v1/turns", params={"since": since}, headers=k)).json()["turns"]
        assert [t["turn_id"] for t in recent] == [ids[2]]
        one = (await c.get("/api/agent/v1/turns?conversation_id=c2", headers=k)).json()["turns"]
        assert [t["turn_id"] for t in one] == [ids[2]]

        # Another key of the same owner, and another owner's key, see none of these.
        assert [t["turn_id"] for t in (await c.get("/api/agent/v1/turns", headers=_bearer(second["key"]))).json()["turns"]] == [ids[3]]
        assert [t["turn_id"] for t in (await c.get("/api/agent/v1/conversations/c1/turns", headers=_bearer(theirs["key"]))).json()["turns"]] == [ids[4]]
        assert (await c.get(f"/api/agent/v1/turns/{ids[0]}", headers=_bearer(theirs["key"]))).status_code == 404

        assert (await c.get("/api/agent/v1/turns")).status_code == 401
        assert (await c.get("/api/agent/v1/turns", headers=_bearer(user))).status_code == 401
        assert (await c.get("/api/agent/v1/conversations/bad id!/turns", headers=k)).status_code == 422
        assert (await c.get("/api/agent/v1/turns?status=done", headers=k)).status_code == 422
        assert (await c.get("/api/agent/v1/turns?limit=500", headers=k)).status_code == 422


# ---------------------------------------------------------------- callback settings

async def test_callback_url_is_validated_secret_shown_once_and_stored_encrypted(db_factory):
    app, state = await _app_with_keys(db_factory)
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        other = await _register(c, "other@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        cid = (await c.post("/api/agent-clients", json={"name": "G"}, headers=_bearer(user))).json()["client"]["id"]
        path = f"/api/agent-clients/{cid}/callback"

        for bad in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://[::1]/x", "https://10.0.0.5/x",
                    "https://8.8.8.8/x", "https://2130706433/x", "https://0x7f.1/x", "https://127.1/x",
                    "https://localhost/x", "https://api.localhost/x", "https://printer.local/x", "https://intranet/x",
                    "ftp://hooks.example.com", "https://u:p@hooks.example.com/x", "not a url"):
            r = await c.put(path, json={"callback_url": bad}, headers=_bearer(user))
            assert r.status_code == 422, bad

        assert (await c.put(path, json={"callback_url": "https://hooks.example.com/x"}, headers=_bearer(other))).status_code == 404

        set_ = (await c.put(path, json={"callback_url": "https://hooks.example.com/x"}, headers=_bearer(user))).json()
        secret = set_["secret"]
        assert secret.startswith("whsec_") and set_["client"]["callback_url"] == "https://hooks.example.com/x"
        async with db_factory() as db:
            stored = (await db.get(AgentClient, cid)).callback_secret
        assert stored.startswith("enc::") and secret not in stored

        moved = (await c.put(path, json={"callback_url": "https://hooks.example.com/y"}, headers=_bearer(user))).json()
        assert "secret" not in moved  # same secret kept
        rotated = (await c.put(path, json={"callback_url": "https://hooks.example.com/y", "rotate_secret": True},
                               headers=_bearer(user))).json()
        assert rotated["secret"] != secret
        assert await state.agent_channel_store.callback_for(cid) == ("https://hooks.example.com/y", rotated["secret"])

        listed = (await c.get("/api/agent-clients", headers=_bearer(user))).json()["clients"][0]
        assert listed["callback_url"] == "https://hooks.example.com/y" and rotated["secret"] not in json.dumps(listed)
        admin_view = (await c.get("/api/admin/agent-channel", headers=_bearer(admin))).json()["clients"][0]
        assert admin_view["callback_url"] is None and admin_view["callback_host"] == "hooks.example.com"
        audit = json.dumps(await state.audit.list())
        assert "agent_channel.key_callback" in audit and rotated["secret"] not in audit

        cleared = (await c.put(path, json={"callback_url": None}, headers=_bearer(user))).json()
        assert cleared["client"]["callback_url"] is None
        assert await state.agent_channel_store.callback_for(cid) is None

        await c.delete(f"/api/agent-clients/{cid}", headers=_bearer(user))
        revoked = await c.put(path, json={"callback_url": "https://hooks.example.com/x"}, headers=_bearer(user))
        assert revoked.status_code == 409


async def test_a_dev_flag_allows_http_callbacks(db_factory):
    app, state = await _app_with_keys(db_factory)
    state.agent_channel.webhook_allow_http = True
    async with client(app) as c:
        admin = await _register(c, "admin@x.io")
        user = await _register(c, "user@x.io")
        await c.put("/api/admin/agent-channel", json={"enabled": True}, headers=_bearer(admin))
        cid = (await c.post("/api/agent-clients", json={"name": "G"}, headers=_bearer(user))).json()["client"]["id"]
        path = f"/api/agent-clients/{cid}/callback"
        assert (await c.put(path, json={"callback_url": "http://hooks.example.com/x"}, headers=_bearer(user))).status_code == 200
        assert (await c.put(path, json={"callback_url": "http://127.0.0.1/x"}, headers=_bearer(user))).status_code == 422


# ---------------------------------------------------------------- delivery

def _recorder(responses):
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return httpx.Response(nxt)

    return seen, (lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_delivery_is_signed_over_timestamp_and_body():
    seen, factory = _recorder([200])
    body = {"turn_id": "t1", "conversation_id": "c", "status": "completed", "reply": "สวัสดี"}
    result = await agent_webhook.deliver("https://hooks.example.com/x", "whsec_s", body, client_factory=factory)
    assert result == ("delivered", 1, None)
    request = seen[0]
    ts = request.headers["X-Claw-Timestamp"]
    expected = hmac.new(b"whsec_s", ts.encode() + b"." + request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Claw-Signature"] == f"sha256={expected}"
    assert json.loads(request.content) == body
    assert abs(int(ts) - int(datetime.now(timezone.utc).timestamp())) < 5


async def test_delivery_retries_server_errors_then_gives_up():
    seen, factory = _recorder([500, httpx.ConnectError("down"), 200])
    assert await agent_webhook.deliver("https://h.example.com/x", "s", {"turn_id": "t"}, backoff=(0, 0),
                                       client_factory=factory) == ("delivered", 3, None)
    assert [r.headers["X-Claw-Delivery-Attempt"] for r in seen] == ["1", "2", "3"]

    seen, factory = _recorder([503, 503, 503])
    assert await agent_webhook.deliver("https://h.example.com/x", "s", {"turn_id": "t"}, backoff=(0, 0),
                                       client_factory=factory) == ("failed", 3, "HTTP 503")


async def test_a_client_error_or_redirect_is_not_retried():
    for code in (400, 401, 302):
        seen, factory = _recorder([code])
        status, attempts, error = await agent_webhook.deliver(
            "https://h.example.com/x", "s", {"turn_id": "t"}, backoff=(0, 0), client_factory=factory
        )
        assert (status, attempts, error) == ("failed", 1, f"HTTP {code}")


async def test_private_targets_are_refused_before_and_after_dns():
    status, attempts, _ = await agent_webhook.deliver("https://169.254.169.254/latest", "s", {"turn_id": "t"})
    assert (status, attempts) == ("failed", 0)
    status, attempts, _ = await agent_webhook.deliver("http://hooks.example.com/x", "s", {"turn_id": "t"})
    assert (status, attempts) == ("failed", 0)  # http without the dev flag
    status, attempts, _ = await agent_webhook.deliver("https://localhost/x", "s", {"turn_id": "t"})
    assert (status, attempts) == ("failed", 0)


async def test_a_public_looking_name_that_resolves_privately_is_caught(monkeypatch):
    import socket

    async def loopback(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", loopback)
    with pytest.raises(agent_webhook.InvalidCallbackUrl):
        await agent_webhook.validate_callback_url("https://rebind.example.com/x")
    # And at send time, by the pinned transport, on every attempt.
    status, attempts, error = await agent_webhook.deliver(
        "https://rebind.example.com/x", "s", {"turn_id": "t"}, backoff=(0, 0)
    )
    assert status == "failed" and "non-public" in error


async def test_each_attempt_has_one_total_timeout_including_dns(monkeypatch):
    import socket

    async def slow_dns(self, host, port, **kwargs):
        await asyncio.sleep(10)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", slow_dns)
    started = asyncio.get_running_loop().time()
    result = await agent_webhook.deliver(
        "https://slow.example.com/x", "s", {"turn_id": "t"}, attempts=2, backoff=(0,), timeout=0.1
    )
    assert result == ("failed", 2, "Timeout")
    assert asyncio.get_running_loop().time() - started < 2


async def test_concurrent_deliveries_are_capped(monkeypatch):
    monkeypatch.setattr(agent_webhook, "MAX_CONCURRENT", 2)
    agent_webhook._slots.clear()
    live = peak = 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1
        return httpx.Response(200)

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    results = await asyncio.gather(*[
        agent_webhook.deliver("https://h.example.com/x", "s", {"turn_id": str(i)}, client_factory=factory)
        for i in range(6)
    ])
    assert all(r[0] == "delivered" for r in results) and peak == 2
    agent_webhook._slots.clear()


# ---------------------------------------------------------------- service

async def test_a_finished_turn_is_delivered_without_holding_up_the_reply(db_factory, stores, monkeypatch):
    runtime = _FakeRuntime(reply="done!")
    service, store, owner, row, _ = await _service(db_factory, stores, runtime)
    _, secret = await store.set_callback(owner.id, row.id, "https://hooks.example.com/x")
    sent: list[tuple] = []
    release = asyncio.Event()

    async def fake_deliver(url, secret_, body, *, allow_http=False):
        sent.append((url, secret_, body, allow_http))
        await release.wait()  # a slow receiver
        return "delivered", 1, None

    monkeypatch.setattr(agent_webhook, "deliver", fake_deliver)
    turn = await asyncio.wait_for(service.send(row, "c", "hi"), timeout=2)
    assert (turn.status, turn.reply) == ("completed", "done!")
    await asyncio.sleep(0.05)
    assert sent and sent[0][:2] == ("https://hooks.example.com/x", secret)
    body = sent[0][2]
    assert (body["turn_id"], body["conversation_id"], body["status"], body["reply"]) == (turn.id, "c", "completed", "done!")
    assert body["finished_at"]
    assert (await store.get_turn(row.id, turn.id)).webhook_status == "pending"
    release.set()
    await asyncio.sleep(0.05)
    final = await store.get_turn(row.id, turn.id)
    assert (final.webhook_status, final.webhook_attempts) == ("delivered", 1)


async def test_a_failed_turn_is_delivered_too_and_a_broken_webhook_never_fails_a_turn(db_factory, stores, monkeypatch):
    from claw.core.runtime import TurnFailed

    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime(fail=TurnFailed("quota")))
    await store.set_callback(owner.id, row.id, "https://hooks.example.com/x")
    bodies = []

    async def exploding(url, secret, body, *, allow_http=False):
        bodies.append(body)
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_webhook, "deliver", exploding)
    turn = await service.send(row, "c", "x")
    await asyncio.sleep(0.05)
    assert (turn.status, turn.reply) == ("failed", "quota")
    assert bodies[0]["status"] == "failed"
    final = await store.get_turn(row.id, turn.id)
    assert final.status == "failed"
    # The crash after "pending" is recorded, not left pending forever.
    assert final.webhook_status == "failed" and final.webhook_error
    assert row.id not in service._busy


async def test_an_undecryptable_secret_is_reported_not_sent(db_factory, stores, monkeypatch):
    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime())
    await store.set_callback(owner.id, row.id, "https://hooks.example.com/x")
    async with db_factory() as db:
        (await db.get(AgentClient, row.id)).callback_secret = "enc::garbage"
        await db.commit()
    calls = []

    async def fake_deliver(*a, **k):
        calls.append(a)
        return "delivered", 1, None

    monkeypatch.setattr(agent_webhook, "deliver", fake_deliver)
    turn = await service.send(row, "c", "x")
    await asyncio.sleep(0.05)
    final = await store.get_turn(row.id, turn.id)
    assert calls == [] and final.webhook_status == "failed" and "rotate" in final.webhook_error


async def test_shutdown_waits_briefly_then_cancels_and_fails_the_turn(db_factory, stores, monkeypatch):
    monkeypatch.setattr(agent_module, "WAIT_SECONDS", 0.05)
    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime(delay=30))
    turn = await service.send(row, "c", "slow")
    assert turn.status == "working" and service._tasks
    await service.aclose(timeout=0.1)
    assert not service._tasks
    final = await store.get_turn(row.id, turn.id)
    assert final.status == "failed"
    # Nothing new starts once shutdown has cancelled the rest.
    late = await service.send(row, "c", "late")
    assert late.status == "failed" and "shutting down" in late.reply


async def test_shutdown_lets_quick_work_finish(db_factory, stores, monkeypatch):
    monkeypatch.setattr(agent_module, "WAIT_SECONDS", 0.01)
    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime(reply="ok", delay=0.1))
    turn = await service.send(row, "c", "quick")
    await service.aclose(timeout=2)
    assert (await store.get_turn(row.id, turn.id)).status == "completed"


async def test_no_callback_means_no_delivery_and_a_revoked_key_gets_none(db_factory, stores, monkeypatch):
    service, store, owner, row, _ = await _service(db_factory, stores, _FakeRuntime())
    calls = []

    async def fake_deliver(*a, **k):
        calls.append(a)
        return "delivered", 1, None

    monkeypatch.setattr(agent_webhook, "deliver", fake_deliver)
    turn = await service.send(row, "c", "x")
    await asyncio.sleep(0.05)
    assert calls == [] and (await store.get_turn(row.id, turn.id)).webhook_status is None
    await store.set_callback(owner.id, row.id, "https://hooks.example.com/x")
    await store.revoke(row.id)
    assert await store.callback_for(row.id) is None
