"""Multimodal content building + attachment upload."""

import base64
from pathlib import Path

from claw.core.context import build_user_content, swap_images_for_description
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


def test_swapping_images_for_a_description_removes_the_stale_pointer(tmp_path):
    """The 'attached below' sentence must go with the images it points at —
    left behind, it tells a text-only model to look at blocks that are no
    longer in the message."""
    (tmp_path / "uploads").mkdir()
    img = tmp_path / "uploads" / "pic.png"
    img.write_bytes(_PNG)
    content, _ = build_user_content("what is this?", [str(img)], tmp_path)

    swapped = swap_images_for_description(content, "A red square.", "vendor/eyes-1", described=1)

    assert isinstance(swapped, str)
    assert "what is this?" in swapped
    assert "attached below" not in swapped
    assert "A red square." in swapped
    assert "vendor/eyes-1" in swapped


def test_swap_says_which_images_went_unread(tmp_path):
    """A cap that bites has to be visible to the chat model — otherwise it
    answers about an image nobody looked at."""
    (tmp_path / "uploads").mkdir()
    paths = []
    for i in range(3):
        img = tmp_path / "uploads" / f"pic{i}.png"
        img.write_bytes(_PNG)
        paths.append(str(img))
    content, _ = build_user_content("what are these?", paths, tmp_path)

    swapped = swap_images_for_description(content, "A red square.", "vendor/eyes-1", described=2)

    assert "3 images attached, but only the first 2 could be read" in swapped
    assert "not read" in swapped


def test_swap_flags_a_description_that_was_cut_off(tmp_path):
    (tmp_path / "uploads").mkdir()
    img = tmp_path / "uploads" / "pic.png"
    img.write_bytes(_PNG)
    content, _ = build_user_content("what is this?", [str(img)], tmp_path)

    swapped = swap_images_for_description(
        content, "A red squ", "vendor/eyes-1", described=1, truncated=True
    )

    assert "cut off" in swapped


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
