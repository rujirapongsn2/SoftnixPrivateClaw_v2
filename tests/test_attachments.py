"""Multimodal content building + attachment upload."""

import base64
from pathlib import Path

from claw.core.context import build_user_content
from tests.conftest_app import build_api_app, client

# 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def test_no_media_returns_plain_text(tmp_path):
    content, stored = build_user_content("hello", None, tmp_path)
    assert content == "hello" and stored == "hello"


def test_image_becomes_inline_block(tmp_path):
    (tmp_path / "uploads").mkdir()
    img = tmp_path / "uploads" / "pic.png"
    img.write_bytes(_PNG)

    content, stored = build_user_content("what is this?", [str(img)], tmp_path)
    assert isinstance(content, list)
    image_blocks = [b for b in content if b["type"] == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # Stored form references the file by name, never the base64 payload.
    assert "pic.png" in stored and "base64" not in stored


def test_non_image_file_becomes_grounding_note(tmp_path):
    (tmp_path / "uploads").mkdir()
    doc = tmp_path / "uploads" / "notes.txt"
    doc.write_text("some content")

    content, stored = build_user_content("summarize", [str(doc)], tmp_path)
    # Plain string content (no image), with a note pointing at the workspace path.
    assert isinstance(content, str)
    assert "uploads/notes.txt" in content and "read_file" in content
    assert "[Attached: notes.txt]" in stored


def test_missing_file_ignored(tmp_path):
    content, stored = build_user_content("hi", [str(tmp_path / "nope.png")], tmp_path)
    assert content == "hi" and stored == "hi"


async def _register(c, email="a@x.io"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]["id"]


async def test_upload_saves_to_workspace_and_returns_refs(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token, uid = await _register(c)
        s = await c.post("/api/sessions", json={"title": "t"}, headers={"Authorization": f"Bearer {token}"})
        sid = s.json()["id"]

        resp = await c.post(
            f"/api/sessions/{sid}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files=[("files", ("pic.png", _PNG, "image/png"))],
        )
        assert resp.status_code == 200
        meta = resp.json()[0]
        assert meta["is_image"] is True and meta["path"].startswith("uploads/")
        # File actually landed in the user's workspace.
        saved = (tmp_path / "ws" / uid / meta["path"])
        assert saved.is_file() and saved.read_bytes() == _PNG


async def test_upload_requires_owned_session(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        token_a, _ = await _register(c, "a@x.io")
        token_b, _ = await _register(c, "b@x.io")
        s = await c.post("/api/sessions", json={"title": "t"}, headers={"Authorization": f"Bearer {token_a}"})
        sid = s.json()["id"]
        # User B cannot upload into user A's session.
        resp = await c.post(
            f"/api/sessions/{sid}/attachments",
            headers={"Authorization": f"Bearer {token_b}"},
            files=[("files", ("x.txt", b"hi", "text/plain"))],
        )
        assert resp.status_code == 404
