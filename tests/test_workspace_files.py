"""Workspace file serving and the table-preview endpoint: tenant isolation,
path-escape rejection, and the CSP that keeps agent-authored HTML from running
as same-origin script."""

import asyncio
import errno
import threading

import pytest

from claw.api import routes
from tests.conftest_app import build_api_app, client


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]["id"]


async def _session(c, token):
    r = await c.post("/api/sessions", json={"title": "t"}, headers={"Authorization": f"Bearer {token}"})
    return r.json()["id"]


def _workspace(tmp_path, uid):
    ws = tmp_path / "ws" / uid
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def test_workspace_file_is_served_with_a_script_blocking_csp(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "report.html").write_text("<h1>hi</h1>", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/files/report.html", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        csp = resp.headers["content-security-policy"]
        # An agent-authored page is untrusted content on the app's own origin:
        # without these it could read cookies/localStorage and call the API.
        assert "script-src 'none'" in csp
        assert "object-src 'none'" in csp
        # default-src does NOT act as a fallback for these two, so a plain
        # <form> or <base> would still work if they were missing.
        assert "form-action 'self'" in csp
        assert "base-uri 'self'" in csp
        # style-src lacked 'self', the only directive weaker than its
        # img-src/font-src siblings for no documented reason.
        assert "style-src 'self'" in csp
        assert resp.headers["x-content-type-options"] == "nosniff"


async def test_workspace_file_rejects_path_escape(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        _workspace(tmp_path, uid)

        # Percent-encoded, because httpx collapses a literal ../ client-side
        # and the request would never reach the route. Starlette decodes this
        # back to ../../secret.txt before _resolve_attachment sees it.
        resp = await c.get(
            f"/api/sessions/{sid}/files/%2e%2e%2f%2e%2e%2fsecret.txt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


async def test_workspace_file_requires_owned_session(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token_a, uid_a = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        sid = await _session(c, token_a)
        (_workspace(tmp_path, uid_a) / "private.csv").write_text("a\n1\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/files/private.csv", headers={"Authorization": f"Bearer {token_b}"}
        )
        assert resp.status_code == 404


async def test_table_preview_returns_bounded_rows(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "out.csv").write_text("name,qty\nwidget,3\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "out.csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["columns"] == ["name", "qty"]
        assert body["rows"] == [["widget", "3"]]
        assert body["truncated"] is False


async def test_table_preview_never_runs_on_the_shared_default_executor(db_factory, tmp_path, monkeypatch):
    # asyncio.to_thread's default pool is shared with the agent's file tools,
    # knowledge ingestion and outbound mail. openpyxl has to materialize
    # sharedStrings.xml in full before the first row is readable, so a handful
    # of concurrent previews there stalls every other tenant's agent for tens of
    # seconds. The parse must land in the dedicated pool instead.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    seen: list[str] = []
    original = routes.preview_table

    def spy(path):
        seen.append(threading.current_thread().name)
        return original(path)

    monkeypatch.setattr(routes, "preview_table", spy)
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "out.csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
    assert seen and all(name.startswith("claw-preview") for name in seen), seen


async def test_table_preview_sheds_load_instead_of_queueing_without_bound(db_factory, tmp_path, monkeypatch):
    # A bounded pool alone only moves the pile-up into its queue: a fast scroll
    # intersects many preview cards at once and each request would then wait for
    # every parse ahead of it. Past the admission depth the endpoint must answer
    # 503 rather than hold the request open indefinitely.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    monkeypatch.setattr(routes, "_PREVIEW_MAX_QUEUED", 1)
    monkeypatch.setattr(routes, "_preview_gate", None)
    monkeypatch.setattr(routes, "_PREVIEW_QUEUE_TIMEOUT", 0.05)
    release = threading.Event()
    original = routes.preview_table

    def blocking(path):
        release.wait(timeout=10)
        return original(path)

    monkeypatch.setattr(routes, "preview_table", blocking)
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        url = f"/api/sessions/{sid}/file-preview"

        held = asyncio.create_task(c.get(url, params={"path": "out.csv"}, headers=headers))
        # Let the first request take the only slot before the second asks.
        while routes._preview_gate is None or not routes._preview_gate.overall.locked():
            await asyncio.sleep(0.01)

        shed = await c.get(url, params={"path": "out.csv"}, headers=headers)
        assert shed.status_code == 503
        assert shed.headers["retry-after"] == "5"

        release.set()
        assert (await held).status_code == 200

    # The slot must come back even though the shed request never held one.
    assert routes._preview_gate.overall.locked() is False


async def test_table_preview_rejects_path_escape(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    (tmp_path / "outside.csv").write_text("a\n1\n", encoding="utf-8")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        _workspace(tmp_path, uid)

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "../../outside.csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


async def test_table_preview_requires_owned_session(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token_a, uid_a = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        sid = await _session(c, token_a)
        (_workspace(tmp_path, uid_a) / "private.csv").write_text("a\n1\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "private.csv"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404


async def test_table_preview_rejects_unsupported_type(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "notes.txt").write_text("hi", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "notes.txt"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


async def test_table_preview_reports_corrupt_file_as_client_error(db_factory, tmp_path):
    # A .xlsx that isn't a zip at all — this must not surface as a 500.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "broken.xlsx").write_bytes(b"not a workbook")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "broken.xlsx"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


async def test_table_preview_reports_a_concurrently_deleted_file_as_404_not_500(
    db_factory, tmp_path, monkeypatch
):
    # _resolve_attachment only checks the file exists once, before the request
    # queues for an executor slot; a delete/overwrite landing in that window
    # must not surface as an unhandled 500.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")

    def vanished(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(routes, "preview_table", vanished)
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "out.csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


async def test_svg_is_treated_as_active_content(db_factory, tmp_path):
    # SVG is not "just an image": it can carry <script> and runs same-origin
    # when opened as a top-level document.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "chart.svg").write_text("<svg/>", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/files/chart.svg", headers={"Authorization": f"Bearer {token}"}
        )
        assert "script-src 'none'" in resp.headers["content-security-policy"]


async def test_pdf_is_not_given_a_csp_that_blocks_the_browser_viewer(db_factory, tmp_path):
    # object-src 'none' also blocks Chromium's built-in PDF viewer, which
    # renders a top-level PDF through an embedded plugin document — and agent
    # generated PDFs are a live feature. A PDF cannot execute same-origin
    # script, so it does not need the policy in the first place.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "report.pdf").write_bytes(b"%PDF-1.4\n")

        resp = await c.get(
            f"/api/sessions/{sid}/files/report.pdf", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "content-security-policy" not in resp.headers
        # nosniff stays unconditional — it is what makes the media-type-based
        # CSP decision trustworthy.
        assert resp.headers["x-content-type-options"] == "nosniff"


async def test_share_file_keeps_no_index_and_no_referrer_headers(db_factory, tmp_path):
    # _share_no_index() sets these on an injected Response, which FastAPI
    # DISCARDS when the handler returns a FileResponse — they must be carried
    # on the FileResponse itself or the share capability token leaks via
    # Referer and the snapshot becomes search-indexable.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, _ = await _register(c)
        sid = await _session(c, token)
        created = await c.post(
            f"/api/sessions/{sid}/share",
            json={"messages": [{"role": "assistant", "content": "hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 200, created.text
        share_token = created.json()["token"]

        share = await app.state.claw.shares.get_active_by_token(share_token, bump=False)
        files_dir = tmp_path / "ws" / "_shares" / share.id / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "report.html").write_text("<h1>hi</h1>", encoding="utf-8")

        resp = await c.get(f"/api/share/{share_token}/files/report.html")
        assert resp.status_code == 200
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["x-robots-tag"] == "noindex, nofollow"
        assert "script-src 'none'" in resp.headers["content-security-policy"]


async def test_html_preview_returns_bounded_markup_as_json(db_factory, tmp_path):
    # JSON, not text/html, so the only thing that renders agent-authored markup
    # is the sandboxed iframe the UI puts it in. nosniff is what makes that hold:
    # current_user accepts `?token=`, so this URL IS navigable as a document, and
    # without it the body could be sniffed into same-origin HTML.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "report.html").write_text("<h1>Q3</h1>", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview/html",
            params={"path": "report.html"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers["x-content-type-options"] == "nosniff"
        body = resp.json()
        assert body["truncated"] is False
        assert body["html"].endswith("<h1>Q3</h1>")
        assert "Content-Security-Policy" in body["html"]


async def test_table_preview_is_also_nosniff(db_factory, tmp_path):
    # Same reasoning, same shared helper — asserted separately so a future
    # refactor can't drop the header from one route and keep it on the other.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "d.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview",
            params={"path": "d.csv"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"


async def test_html_preview_maps_an_unreadable_path_to_a_client_error(db_factory, tmp_path):
    """_resolve_attachment only checks is_file(); open() happens after a queue
    wait and a thread hop, so the path can become a directory in between.
    IsADirectoryError is not a FileNotFoundError, so it used to escape as a 500."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        target = _workspace(tmp_path, uid) / "report.html"
        target.write_text("<p>x</p>", encoding="utf-8")

        original = routes._resolve_attachment

        def swap(workspace, path):
            resolved = original(workspace, path)
            target.unlink()
            target.mkdir()
            return resolved

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(routes, "_resolve_attachment", swap)
            resp = await c.get(
                f"/api/sessions/{sid}/file-preview/html",
                params={"path": "report.html"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 400
        # The header the success path sets on the injected Response is gone by
        # the time an HTTPException unwinds past it — see _PREVIEW_ERROR_HEADERS.
        assert resp.headers["x-content-type-options"] == "nosniff"


async def test_preview_error_bodies_are_also_nosniff(db_factory, tmp_path):
    """Every preview error status, not just the 200. These bodies echo a parser
    message or the caller's own `path` back inside JSON, and the URL is navigable
    because current_user accepts `?token=` — so a sniffable error body is the
    same same-origin HTML hazard the success path already guards against."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        hdr = {"Authorization": f"Bearer {token}"}
        (_workspace(tmp_path, uid) / "notes.txt").write_text("hi", encoding="utf-8")
        (_workspace(tmp_path, uid) / "broken.xlsx").write_bytes(b"not a workbook")

        for path, expected in [("notes.txt", 400), ("broken.xlsx", 400), ("gone.csv", 404)]:
            resp = await c.get(f"/api/sessions/{sid}/file-preview", params={"path": path}, headers=hdr)
            assert resp.status_code == expected, path
            assert resp.headers["x-content-type-options"] == "nosniff", path


async def test_preview_ownership_404_is_also_nosniff(db_factory, tmp_path):
    """The ownership check runs inside the preview handler but raises before it
    returns, so it does not inherit the nosniff set on the injected Response —
    it has to carry the header itself. Asserted for both preview kinds because
    they reach _owned_session through the same helper and would regress together."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token_a, uid_a = await _register(c, "own@x.io")
        token_b, _ = await _register(c, "other@x.io")
        sid = await _session(c, token_a)
        (_workspace(tmp_path, uid_a) / "private.csv").write_text("a\n1\n", encoding="utf-8")
        (_workspace(tmp_path, uid_a) / "private.html").write_text("<p>x</p>", encoding="utf-8")
        hdr = {"Authorization": f"Bearer {token_b}"}

        for url, path in [("file-preview", "private.csv"), ("file-preview/html", "private.html")]:
            resp = await c.get(f"/api/sessions/{sid}/{url}", params={"path": path}, headers=hdr)
            assert resp.status_code == 404, url
            assert resp.headers["x-content-type-options"] == "nosniff", url


async def test_preview_does_not_report_host_exhaustion_as_a_client_error(db_factory, tmp_path):
    """OSError covers both "this file is unreadable" and "this host is out of
    descriptors". Mapping the whole class to 400 told the client the artifact was
    permanently broken, so the UI latched its terminal chip fallback and no retry
    applied — while an EMFILE leak stayed invisible in the 5xx rate."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")

    def exhausted(path):
        raise OSError(errno.EMFILE, "Too many open files")

    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(routes, "preview_table", exhausted)
            with pytest.raises(OSError, match="Too many open files"):
                await c.get(
                    f"/api/sessions/{sid}/file-preview",
                    params={"path": "out.csv"},
                    headers={"Authorization": f"Bearer {token}"},
                )


async def test_html_preview_rejects_a_non_html_path(db_factory, tmp_path):
    # The endpoint resolves any workspace-relative path, so without the suffix
    # check it would be a general "read any of my files as text" endpoint —
    # which is a very different capability from previewing an artifact.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "notes.txt").write_text("secret", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview/html",
            params={"path": "notes.txt"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


async def test_html_preview_requires_owned_session(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token_a, uid_a = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        sid = await _session(c, token_a)
        (_workspace(tmp_path, uid_a) / "private.html").write_text("<p>x</p>", encoding="utf-8")

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview/html",
            params={"path": "private.html"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404


async def test_html_preview_rejects_path_escape(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    (tmp_path / "outside.html").write_text("<p>nope</p>", encoding="utf-8")
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        _workspace(tmp_path, uid)

        resp = await c.get(
            f"/api/sessions/{sid}/file-preview/html",
            params={"path": "../../outside.html"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


async def test_html_preview_sheds_load_like_the_table_preview(db_factory, tmp_path, monkeypatch):
    # Both preview kinds share one admission gate; a new kind that bypassed it
    # would reintroduce the unbounded queue the gate exists to prevent.
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    monkeypatch.setattr(routes, "_PREVIEW_MAX_QUEUED", 1)
    monkeypatch.setattr(routes, "_preview_gate", None)
    monkeypatch.setattr(routes, "_PREVIEW_QUEUE_TIMEOUT", 0.05)
    release = threading.Event()
    original = routes.preview_html

    def blocking(path):
        release.wait(timeout=10)
        return original(path)

    monkeypatch.setattr(routes, "preview_html", blocking)
    async with client(app) as c:
        token, uid = await _register(c)
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "report.html").write_text("<p>x</p>", encoding="utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        url = f"/api/sessions/{sid}/file-preview/html"

        held = asyncio.create_task(c.get(url, params={"path": "report.html"}, headers=headers))
        while routes._preview_gate is None or not routes._preview_gate.overall.locked():
            await asyncio.sleep(0.01)

        shed = await c.get(url, params={"path": "report.html"}, headers=headers)
        assert shed.status_code == 503

        release.set()
        assert (await held).status_code == 200


async def test_one_user_cannot_starve_another_of_preview_slots(db_factory, tmp_path, monkeypatch):
    """The global gate alone is first-come-first-served across tenants, so a
    single user scrolling an artifact-heavy transcript could hold every slot and
    shed everyone else. The per-user cap is what bounds one tenant's share."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    monkeypatch.setattr(routes, "_PREVIEW_MAX_QUEUED", 4)
    monkeypatch.setattr(routes, "_PREVIEW_MAX_PER_USER", 1)
    monkeypatch.setattr(routes, "_preview_gate", None)
    monkeypatch.setattr(routes, "_PREVIEW_QUEUE_TIMEOUT", 0.05)
    release = threading.Event()
    started = threading.Event()
    original = routes.preview_html

    def blocking(path):
        started.set()
        release.wait(timeout=10)
        return original(path)

    monkeypatch.setattr(routes, "preview_html", blocking)
    async with client(app) as c:
        token_a, uid_a = await _register(c, "a@x.io")
        token_b, uid_b = await _register(c, "b@x.io")
        sid_a = await _session(c, token_a)
        sid_b = await _session(c, token_b)
        for uid in (uid_a, uid_b):
            (_workspace(tmp_path, uid) / "r.html").write_text("<p>x</p>", encoding="utf-8")

        hdr_a = {"Authorization": f"Bearer {token_a}"}
        held = asyncio.create_task(
            c.get(f"/api/sessions/{sid_a}/file-preview/html", params={"path": "r.html"}, headers=hdr_a)
        )
        # The global semaphore still has free permits here, so .locked() would
        # never become true — wait on the parse actually starting instead.
        while not started.is_set():
            await asyncio.sleep(0.01)

        # User A is at their cap, so A's own second request sheds...
        mine = await c.get(
            f"/api/sessions/{sid_a}/file-preview/html", params={"path": "r.html"}, headers=hdr_a
        )
        assert mine.status_code == 503

        # ...but user B still gets in, because 3 global slots remain free.
        release.set()
        theirs = await c.get(
            f"/api/sessions/{sid_b}/file-preview/html",
            params={"path": "r.html"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert theirs.status_code == 200
        assert (await held).status_code == 200

    # Per-user entries are dropped once a user has nothing in flight, so this
    # dict tracks concurrent previewers rather than growing once per user id.
    assert routes._preview_gate.per_user == {}


async def test_preview_admits_on_an_idle_gate_with_no_time_budget_left(db_factory, tmp_path, monkeypatch):
    """Shedding must be driven by contention, not by the clock. The per-user and
    global waits share one deadline, so the global acquire routinely sees a
    non-positive remainder — and asyncio.wait_for raises on a non-positive
    timeout without ever polling the awaitable. A zero budget against a
    completely idle gate is that case in its purest form: it has to admit."""
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    monkeypatch.setattr(routes, "_preview_gate", None)
    monkeypatch.setattr(routes, "_PREVIEW_QUEUE_TIMEOUT", 0.0)
    async with client(app) as c:
        token, uid = await _register(c, "idle@x.io")
        sid = await _session(c, token)
        (_workspace(tmp_path, uid) / "r.html").write_text("<p>x</p>", encoding="utf-8")
        res = await c.get(
            f"/api/sessions/{sid}/file-preview/html",
            params={"path": "r.html"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    assert "<p>x</p>" in res.json()["html"]
