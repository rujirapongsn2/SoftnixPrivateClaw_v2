"""Files view: search, kind filter, sort and paging over the workspace listing."""
import os
import threading
import time

from claw.workspace.browse import ListingCache, browse, kind_of
from tests.conftest_app import build_api_app, client
from tests.test_file_lifecycle import setup, write


def _entry(path, size=1, modified_at=0.0, zone="ai"):
    return {"path": path, "size": size, "modified_at": modified_at, "zone": zone, "expires_at": None,
            "kind": kind_of(path)}


def test_kind_of_maps_extensions_case_insensitively():
    assert kind_of("reports/Q3.HTML") == "web"
    assert kind_of("data/login.csv") == "spreadsheet"
    assert kind_of("card.a2ui") == "card"
    assert kind_of("notes") == "other"


def test_browse_searches_counts_filters_sorts_and_pages():
    files = [
        _entry("sales/q3-summary.html", 30, 3),
        _entry("sales/q3.csv", 10, 2),
        _entry("chart.png", 20, 1),
        _entry("รายงาน/สรุป-q3.pdf", 40, 4),
    ]

    page = browse(files, q="Q3", kind="", sort="recent", offset=0, limit=2)
    assert [f["path"] for f in page["files"]] == ["รายงาน/สรุป-q3.pdf", "sales/q3-summary.html"]
    assert page["total"] == 3 and page["next_offset"] == 2
    assert page["counts"] == {"web": 1, "spreadsheet": 1, "document": 1}

    rest = browse(files, q="q3", kind="", sort="recent", offset=2, limit=2)
    assert [f["path"] for f in rest["files"]] == ["sales/q3.csv"] and rest["next_offset"] is None

    only_csv = browse(files, q="", kind="spreadsheet", sort="name", offset=0, limit=10)
    assert [f["path"] for f in only_csv["files"]] == ["sales/q3.csv"]
    assert only_csv["counts"]["image"] == 1

    by_size = browse(files, q="", kind="", sort="size", offset=0, limit=10)
    assert [f["size"] for f in by_size["files"]] == [40, 30, 20, 10]


def test_search_matches_every_term_in_any_order_across_separators():
    files = [_entry("sales/q3-summary.html"), _entry("sales/Q3_forecast.xlsx"), _entry("hr/q3-summary.html")]

    page = browse(files, q="Summary  q3 SALES", kind="", sort="name", offset=0, limit=10)

    assert [f["path"] for f in page["files"]] == ["sales/q3-summary.html"]


def test_files_named_after_the_search_come_before_folder_matches():
    files = [
        _entry("reports/old.csv", modified_at=3),
        _entry("archive/reports-2025.pdf", modified_at=1),
        _entry("misc/reports.md", modified_at=2),
    ]

    page = browse(files, q="reports", kind="", sort="recent", offset=0, limit=10)

    assert [f["path"] for f in page["files"]] == ["misc/reports.md", "archive/reports-2025.pdf", "reports/old.csv"]


def test_since_and_zone_narrow_the_listing_and_its_counts():
    files = [
        _entry("old.csv", modified_at=100),
        _entry("new.csv", modified_at=300),
        _entry("upload.png", modified_at=300, zone="user"),
    ]

    recent_ai = browse(files, q="", kind="", sort="recent", offset=0, limit=10, since=200, zone="ai")

    assert [f["path"] for f in recent_ai["files"]] == ["new.csv"]
    assert recent_ai["counts"] == {"spreadsheet": 1}


def test_listing_cache_reuses_a_walk_until_forgotten_or_refreshed():
    walks = []
    cache = ListingCache(ttl=60)

    def walk():
        walks.append(1)
        return {"files": [], "truncated": False}

    cache.get(("m", "u"), walk)
    cache.get(("m", "u"), walk)
    assert len(walks) == 1
    cache.get(("m", "u"), walk, fresh=True)
    cache.forget(("m", "u"))
    cache.get(("m", "u"), walk)
    assert len(walks) == 3


def test_a_walk_overlapping_a_forget_is_not_cached():
    cache = ListingCache(ttl=60)
    stale = {"files": ["trashed.csv"], "truncated": False}

    def walk_then_trash():
        cache.forget(("m", "u"))
        return stale

    assert cache.get(("m", "u"), walk_then_trash) is stale
    fresh_listing = {"files": [], "truncated": False}
    assert cache.get(("m", "u"), lambda: fresh_listing) is fresh_listing


def test_concurrent_misses_share_one_walk():
    cache = ListingCache(ttl=60)
    started, release, walks = threading.Event(), threading.Event(), []

    def slow_walk():
        walks.append(1)
        started.set()
        release.wait(5)
        return {"files": [], "truncated": False}

    first = threading.Thread(target=lambda: cache.get(("m", "u"), slow_walk))
    first.start()
    started.wait(5)
    second = threading.Thread(target=lambda: cache.get(("m", "u"), slow_walk))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert len(walks) == 1


async def test_files_api_pages_newest_first_and_drops_trashed_files(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        now = time.time()
        for i, name in enumerate(["old.csv", "mid.html", "new.png"]):
            os.utime(write(app, uid, name), (now - 100 + i, now - 100 + i))

        first = (await c.get("/api/files?limit=2&refresh=true", headers=headers)).json()
        assert [f["path"] for f in first["files"]] == ["new.png", "mid.html"]
        assert first["total"] == 3 and first["next_offset"] == 2 and first["truncated"] is False
        assert first["walk_limit"] == 20_000
        assert first["files"][0]["kind"] == "image"

        await c.post("/api/files/trash", headers=headers, json={"path": "new.png"})
        after = (await c.get("/api/files", headers=headers)).json()
        assert [f["path"] for f in after["files"]] == ["mid.html", "old.csv"]

        assert (await c.get("/api/files?kind=bogus", headers=headers)).status_code == 422
        assert (await c.get("/api/files?zone=system", headers=headers)).status_code == 422
        recent = (await c.get(f"/api/files?since={now - 99.5}", headers=headers)).json()
        assert [f["path"] for f in recent["files"]] == ["mid.html"]


async def test_files_api_listing_is_per_user(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "ws")
    async with client(app) as c:
        owner, owner_headers, _ = await setup(c)
        _, other_headers, _ = await setup(c, "other@example.test")
        write(app, owner, "private.csv")
        assert (await c.get("/api/files", headers=owner_headers)).json()["total"] == 1
        assert (await c.get("/api/files", headers=other_headers)).json()["total"] == 0
