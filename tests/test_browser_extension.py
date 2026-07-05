"""Client browser-extension broker + API."""

import json

from claw.browser.broker import BrowserBrokerStore
from tests.conftest_app import build_api_app, client


# ---- broker unit tests ----------------------------------------------------


def test_broker_pairing_roundtrip_and_token_is_hashed(tmp_path):
    broker = BrowserBrokerStore(tmp_path / "broker")
    ticket = broker.create_pairing(user_id="u1", label="me")["ticket"]
    paired = broker.complete_pairing(ticket=ticket)
    assert paired["extension_token"]  # raw token returned once
    assert paired["instance_id"] == "u1"  # extension needs a truthy instance id

    # Raw token is never persisted — only its hash.
    stored = json.loads((tmp_path / "broker" / "extensions.json").read_text())
    record = stored[paired["extension_id"]]
    assert "token_hash" in record and paired["extension_token"] not in json.dumps(stored)

    # A reused ticket is rejected.
    try:
        broker.complete_pairing(ticket=ticket)
        assert False, "reused ticket should fail"
    except ValueError:
        pass


def test_broker_task_roundtrip_and_redaction(tmp_path):
    broker = BrowserBrokerStore(tmp_path / "broker")
    ticket = broker.create_pairing(user_id="u1")["ticket"]
    paired = broker.complete_pairing(ticket=ticket)
    ext = broker.authenticate_extension(
        extension_id=paired["extension_id"], extension_token=paired["extension_token"]
    )

    task = broker.enqueue_task({"action": "fill", "user_id": "u1", "fields": {"password": "hunter2"}})
    # Sensitive values scrubbed before hitting disk.
    on_disk = (tmp_path / "broker" / "tasks" / f"{task['task_id']}.json").read_text()
    assert "hunter2" not in on_disk and "[redacted]" in on_disk

    polled = broker.poll_task(extension=ext)
    assert polled and polled["task_id"] == task["task_id"] and polled["status"] == "running"
    # Second poll returns nothing (task now running, not pending).
    assert broker.poll_task(extension=ext) is None

    broker.submit_result(task_id=task["task_id"], extension=ext, result={"status": "completed", "summary": "ok"})
    result = broker.read_result(task["task_id"])
    assert result and result["summary"] == "ok"


def test_broker_bad_token_rejected(tmp_path):
    broker = BrowserBrokerStore(tmp_path / "broker")
    ticket = broker.create_pairing(user_id="u1")["ticket"]
    paired = broker.complete_pairing(ticket=ticket)
    try:
        broker.authenticate_extension(extension_id=paired["extension_id"], extension_token="wrong")
        assert False, "bad token should raise"
    except PermissionError:
        pass


def test_broker_isolates_users(tmp_path):
    broker = BrowserBrokerStore(tmp_path / "broker")
    ext_a = broker.complete_pairing(ticket=broker.create_pairing(user_id="a")["ticket"])
    auth_a = broker.authenticate_extension(extension_id=ext_a["extension_id"], extension_token=ext_a["extension_token"])
    broker.enqueue_task({"action": "open", "user_id": "b", "url": "https://x"})
    # A's extension must not see B's task.
    assert broker.poll_task(extension=auth_a) is None


# ---- API tests ------------------------------------------------------------


async def _register(c, email, password="password123"):
    r = await c.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_pairing_and_poll_flow_via_api(db_factory):
    app = build_api_app(db_factory, browser={"client_extension_enabled": True})
    broker = app.state.claw.browser_broker
    async with client(app) as c:
        token, user = await _register(c, "u@x.io")

        init = await c.post("/api/browser-extension/pairing/init", headers=_bearer(token))
        assert init.status_code == 200
        body = init.json()
        assert body["instance_id"] == user["id"] and body["pairing_ticket"].startswith("brp_")

        complete = await c.post(
            "/api/browser-extension/pairing/complete",
            json={"instance_id": body["instance_id"], "pairing_ticket": body["pairing_ticket"]},
        )
        assert complete.status_code == 200
        ext = complete.json()
        assert ext["extension_id"].startswith("bre_") and ext["extension_token"]

        # Agent enqueues a task for this user; the extension should receive it.
        broker.enqueue_task({"action": "open", "user_id": user["id"], "url": "https://example.com"})
        poll = await c.post(
            "/api/browser-extension/tasks/poll",
            json={
                "instance_id": ext["instance_id"],
                "extension_id": ext["extension_id"],
                "extension_token": ext["extension_token"],
            },
        )
        assert poll.status_code == 200
        task = poll.json()["task"]
        assert task and task["action"] == "open"

        result = await c.post(
            "/api/browser-extension/tasks/result",
            json={
                "extension_id": ext["extension_id"],
                "extension_token": ext["extension_token"],
                "task_id": task["task_id"],
                "result": {"status": "completed", "summary": "opened"},
            },
        )
        assert result.status_code == 200

        status = await c.get("/api/browser-extension/status", headers=_bearer(token))
        assert status.json()["paired"] is True and status.json()["client_extension_enabled"] is True


async def test_poll_bad_token_forbidden(db_factory):
    app = build_api_app(db_factory, browser={"client_extension_enabled": True})
    async with client(app) as c:
        r = await c.post(
            "/api/browser-extension/tasks/poll",
            json={"extension_id": "bre_nope", "extension_token": "bad"},
        )
        assert r.status_code == 403


async def test_download_returns_zip(db_factory):
    app = build_api_app(db_factory, browser={"client_extension_enabled": True})
    async with client(app) as c:
        r = await c.get("/api/browser-extension/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert r.content[:2] == b"PK"  # zip magic


async def test_disabled_server_hides_feature(db_factory):
    app = build_api_app(db_factory)  # client_extension_enabled defaults False
    async with client(app) as c:
        token, _ = await _register(c, "u@x.io")
        init = await c.post("/api/browser-extension/pairing/init", headers=_bearer(token))
        assert init.status_code == 400
        status = await c.get("/api/browser-extension/status", headers=_bearer(token))
        assert status.json()["client_extension_enabled"] is False
