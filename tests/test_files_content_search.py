"""Files view content search: text extraction, the text store, the background
sync, and the API. SQLite stores, temp workspaces, synthetic files only."""
import asyncio

from openpyxl import Workbook

import claw.api.files as files_api
from claw.db.stores import WorkspaceFileTextStore
import claw.workspace.content_index as content_index
from claw.workspace.content_index import MAX_FILE_BYTES, ContentIndexer, extract_isolated, indexable
from claw.workspace.extract_worker import extract
from tests.conftest_app import build_api_app, client
from tests.test_file_lifecycle import setup, write


def _entry(path, size=10, modified_at=1.0, zone="ai"):
    return {"path": path, "size": size, "modified_at": modified_at, "zone": zone, "expires_at": None}


def test_extract_reads_text_html_and_xlsx_and_survives_a_broken_file(tmp_path):
    (tmp_path / "a.csv").write_text("name,dept\nสมชาย,การเงิน\n", encoding="utf-8")
    (tmp_path / "b.html").write_text("<html><body><h1>Q3 รายงาน</h1><script>x()</script></body></html>")
    book = Workbook()
    book.active.title = "Budget"
    book.active.append(["Marketing", 1200])
    book.save(tmp_path / "c.xlsx")
    (tmp_path / "d.docx").write_bytes(b"not a zip")

    assert "สมชาย" in extract(tmp_path / "a.csv")
    assert "Q3 รายงาน" in extract(tmp_path / "b.html")
    assert "Budget" in extract(tmp_path / "c.xlsx") and "Marketing 1200" in extract(tmp_path / "c.xlsx")
    assert extract(tmp_path / "d.docx") == ""


def test_indexable_skips_images_scratch_and_large_files():
    assert indexable(_entry("report.pdf"))
    assert not indexable(_entry("chart.png"))
    assert not indexable(_entry("tmp/notes.md", zone="scratch"))
    assert not indexable(_entry("big.csv", size=MAX_FILE_BYTES + 1))


async def test_store_finds_files_holding_every_term_with_an_excerpt(db_factory):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    await store.put("u1", "privateclaw", "a.txt", 1.0, 10, "x" * 200 + " ยอดขาย ไตรมาส 3 ของแผนก Sales " + "y" * 200)
    await store.put("u1", "privateclaw", "b.txt", 1.0, 10, "ยอดขาย only")
    await store.put("u2", "privateclaw", "c.txt", 1.0, 10, "ยอดขาย ไตรมาส")
    await store.put("u1", "sbot", "d.txt", 1.0, 10, "ยอดขาย ไตรมาส")

    found, capped = await store.search("u1", "privateclaw", ["ไตรมาส", "SALES"])

    assert list(found) == ["a.txt"] and capped is False
    assert "ยอดขาย ไตรมาส 3" in found["a.txt"] and len(found["a.txt"]) <= 180


async def test_store_treats_like_wildcards_literally(db_factory):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    await store.put("u1", "m", "a.txt", 1.0, 1, "growth 50% yoy")
    await store.put("u1", "m", "b.txt", 1.0, 1, "growth 500 yoy")

    assert list((await store.search("u1", "m", ["50%"]))[0]) == ["a.txt"]
    assert (await store.search("u1", "m", ["_"]))[0] == {}


async def test_store_returns_the_newest_matches_and_says_when_it_capped(db_factory):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    for i in range(5):
        await store.put("u1", "m", f"f{i}.txt", float(i), 1, "invoice")

    found, capped = await store.search("u1", "m", ["invoice"], limit=3)

    assert list(found) == ["f4.txt", "f3.txt", "f2.txt"] and capped is True


def _listing(files, truncated=False, walked_at=1.0):
    return {"files": files, "truncated": truncated, "walked_at": walked_at}


async def test_sync_indexes_changed_files_and_drops_rows_for_files_that_are_gone(db_factory, tmp_path):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    indexer = ContentIndexer()
    (tmp_path / "keep.md").write_text("alpha")
    (tmp_path / "edit.md").write_text("beta v2")
    (tmp_path / "unlisted.md").write_text("past the walk cap")
    await store.put("u1", "m", "edit.md", 1.0, 1, "beta v1")
    await store.put("u1", "m", "gone.md", 1.0, 1, "gamma")
    await store.put("u1", "m", "unlisted.md", 1.0, 1, "past the walk cap")
    files = [_entry("keep.md", modified_at=5.0), _entry("edit.md", modified_at=6.0)]

    # A walk cut short cannot prove a file is gone, so the disk decides.
    await indexer._sync(store, "m", "u1", tmp_path, _listing(files, truncated=True))
    assert set(await store.indexed("u1", "m")) == {"keep.md", "edit.md", "unlisted.md"}
    assert (await store.search("u1", "m", ["v2"]))[0] != {}

    await indexer._sync(store, "m", "u1", tmp_path, _listing(files))
    assert await store.indexed("u1", "m") == {"keep.md": 5.0, "edit.md": 6.0}


async def test_one_failing_file_does_not_stop_the_rest(db_factory, tmp_path, monkeypatch):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    (tmp_path / "bad.md").write_text("x")
    (tmp_path / "good.md").write_text("fine")
    real_put = store.put

    async def flaky_put(owner, mode, path, *rest):
        if path == "bad.md":
            raise RuntimeError("write failed")
        await real_put(owner, mode, path, *rest)

    monkeypatch.setattr(store, "put", flaky_put)
    files = [_entry("bad.md", modified_at=9.0), _entry("good.md", modified_at=1.0)]

    await ContentIndexer()._sync(store, "m", "u1", tmp_path, _listing(files))

    assert set(await store.indexed("u1", "m")) == {"good.md"}


async def test_a_file_that_hangs_the_parser_is_killed_and_indexed_empty(tmp_path, monkeypatch):
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(content_index, "_WORKER_ARGS", (str(hang),))
    monkeypatch.setattr(content_index, "EXTRACT_TIMEOUT_S", 0.5)

    assert await extract_isolated(tmp_path / "anything.pdf") == ""


async def test_extract_isolated_runs_the_real_worker(tmp_path):
    (tmp_path / "a.md").write_text("ข้อความ Q3", encoding="utf-8")
    assert await extract_isolated(tmp_path / "a.md") == "ข้อความ Q3"


async def test_schedule_syncs_once_per_walk(db_factory, tmp_path):
    store = WorkspaceFileTextStore(db_factory, is_postgres=False)
    indexer = ContentIndexer()
    listing = _listing([], walked_at=7.0)

    indexer.schedule(store, "m", "u1", tmp_path, listing)
    first = indexer._running[("m", "u1")]
    await first
    indexer.schedule(store, "m", "u1", tmp_path, listing)
    assert ("m", "u1") not in indexer._running
    indexer.schedule(store, "m", "u1", tmp_path, _listing([], walked_at=8.0))
    assert indexer._running[("m", "u1")] is not first
    await indexer.shutdown()


async def test_files_api_finds_a_file_by_its_content_for_its_owner_only(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        owner, owner_headers, _ = await setup(c)
        _, other_headers, _ = await setup(c, "other@example.test")
        write(app, owner, "data/export.csv", "employee,reason\nสมหญิง,ลาพักร้อน\n")
        write(app, owner, "notes/ลาพักร้อน.md", "ไม่มีเนื้อหา")

        await c.get("/api/files?refresh=true", headers=owner_headers)
        await asyncio.gather(*files_api._indexer._running.values())
        settled = (await c.get("/api/files", headers=owner_headers)).json()
        assert settled["indexing"] is False and files_api._indexer._running == {}

        found = (await c.get("/api/files?q=ลาพักร้อน", headers=owner_headers)).json()
        assert [(f["path"], f["match"]) for f in found["files"]] == [
            ("notes/ลาพักร้อน.md", "name"), ("data/export.csv", "content"),
        ]
        assert "สมหญิง,ลาพักร้อน" in found["files"][1]["snippet"] and found["content_capped"] is False
        # Names and contents split a search the same way: "employee-reason" is two terms.
        assert [f["path"] for f in (await c.get("/api/files?q=reason-employee", headers=owner_headers)).json()["files"]] == [
            "data/export.csv"]

        await c.get("/api/files?refresh=true", headers=other_headers)
        await asyncio.gather(*files_api._indexer._running.values())
        assert (await c.get("/api/files?q=ลาพักร้อน", headers=other_headers)).json()["files"] == []
