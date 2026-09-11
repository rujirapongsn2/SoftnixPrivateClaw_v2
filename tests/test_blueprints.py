"""Shared Blueprint library, with mode-local sessions and writable copies."""

from dataclasses import replace

import pytest
from fastapi import FastAPI

from sbot.api.blueprints import router
from sbot.db.stores import BlueprintStore, SessionStore
from tests.conftest_app import build_api_app, client


@pytest.mark.asyncio
async def test_blueprints_cross_mode_and_access(db_factory, tmp_path):
    app = build_api_app(db_factory, auth_mode="dev", sbot_enabled=False,
                        workspaces_root=tmp_path / "normal", blueprints_root=tmp_path / "library")
    app.state.claw.blueprints = BlueprintStore(db_factory)
    app.include_router(router)
    mode = FastAPI()
    mode.state.sbot = replace(
        app.state.claw,
        settings=app.state.claw.settings.model_copy(update={"workspaces_root": tmp_path / "bots"}),
        sessions=SessionStore(db_factory, is_postgres=False),
    )
    mode.include_router(router)
    app.mount("/modes/sbot", mode)
    owner = await app.state.claw.users.get_or_create_by_email("owner@example.com")
    normal_session = await app.state.claw.sessions.create(owner.id)
    bot_session = await mode.state.sbot.sessions.create(owner.id)
    headers = {"Authorization": "Bearer t", "X-User-Email": owner.email}
    async with client(app) as http:
        assert (await http.get("/api/blueprints")).status_code == 401
        created = await http.post("/api/blueprints", headers=headers,
                                  data={"name": "Quote", "visibility": "private"},
                                  files={"file": ("quote.docx", b"original template")})
        assert created.status_code == 200, created.text
        bp = created.json()
        mode_list = await http.get("/modes/sbot/api/blueprints", headers=headers)
        assert [row["id"] for row in mode_list.json()] == [bp["id"]]
        for prefix, state, session in [("", app.state.claw, normal_session),
                                      ("/modes/sbot", mode.state.sbot, bot_session)]:
            base = f"{prefix}/api/blueprints"
            copied = await http.post(f"{base}/{bp['id']}/materialize", headers=headers, json={})
            assert copied.status_code == 200, copied.text
            ref = copied.json()
            path = state.settings.workspaces_root / owner.id / ref["path"]
            assert path.read_bytes() == b"original template"
            path.write_bytes(b"edited copy")
            saved = await http.post(f"{base}/from-artifact", headers=headers,
                                   json={"session_id": session.id, "path": ref["path"], "name": "Edited"})
            assert saved.status_code == 200, saved.text
            wrong_session = bot_session if not prefix else normal_session
            rejected = await http.post(f"{base}/from-artifact", headers=headers,
                                      json={"session_id": wrong_session.id, "path": ref["path"], "name": "Wrong mode"})
            assert rejected.status_code == 404
            traversal = await http.post(f"{base}/from-artifact", headers=headers,
                                       json={"session_id": session.id, "path": "../escape.docx", "name": "Escape"})
            assert traversal.status_code == 404
        original = await http.get(f"/api/blueprints/{bp['id']}/versions/1/file", headers=headers)
        assert original.content == b"original template"
        stranger = {**headers, "X-User-Email": "stranger@example.com"}
        assert (await http.get("/api/blueprints", headers=stranger)).json() == []
        denied = await http.post(f"/api/blueprints/{bp['id']}/materialize", headers=stranger, json={})
        assert denied.status_code == 403
