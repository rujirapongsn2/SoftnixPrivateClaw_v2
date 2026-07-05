"""schedule + document agent tools."""

import struct
import zlib
from pathlib import Path

from claw.db.stores import ScheduleStore
from claw.tools.documents import ReadCsvTool, ReadExcelTool
from claw.tools.schedule import ScheduleTool


class FakeScheduler:
    def __init__(self):
        self.notified = 0

    def notify_changed(self):
        self.notified += 1


# ---------------------------------------------------------------- schedule tool

async def test_schedule_tool_create_list_delete(db_factory, stores):
    store = ScheduleStore(db_factory)
    sched = FakeScheduler()
    user = await stores["users"].get_or_create_by_email("s@x.y")
    tool = ScheduleTool(store, sched, user.id)

    out = await tool.execute(action="create", name="Daily report", prompt="summarize", cron="0 9 * * *")
    assert "Scheduled 'Daily report'" in out and sched.notified == 1

    listing = await tool.execute(action="list")
    assert "Daily report" in listing

    rows = await store.list_for_user(user.id)
    out = await tool.execute(action="delete", schedule_id=rows[0].id)
    assert out.startswith("Deleted task") and sched.notified == 2


async def test_schedule_tool_validates(db_factory, stores):
    tool = ScheduleTool(ScheduleStore(db_factory), FakeScheduler(), "u1")
    assert (await tool.execute(action="create", name="x")).startswith("Error: create requires a prompt")
    assert (await tool.execute(action="create", prompt="p", cron="not a cron")).startswith("Error:")
    assert (await tool.execute(action="delete")).startswith("Error: provide")


async def test_schedule_tool_interval(db_factory, stores):
    store = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("s2@x.y")
    tool = ScheduleTool(store, FakeScheduler(), user.id)
    await tool.execute(action="create", name="hourly", prompt="check", interval_minutes=60)
    rows = await store.list_for_user(user.id)
    assert rows[0].interval_seconds == 3600


async def test_schedule_update_by_name_does_not_duplicate(db_factory, stores):
    store = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("upd@x.y")
    tool = ScheduleTool(store, FakeScheduler(), user.id)

    await tool.execute(action="create", name="report", prompt="do it", cron="0 9 * * *")
    # "change the time" via update — must edit in place, not add a row.
    out = await tool.execute(action="update", name="report", cron="0 18 * * *")
    assert "report" in out
    rows = await store.list_for_user(user.id)
    assert len(rows) == 1
    assert rows[0].cron == "0 18 * * *"


async def test_schedule_create_same_name_upserts(db_factory, stores):
    store = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("ups@x.y")
    tool = ScheduleTool(store, FakeScheduler(), user.id)
    await tool.execute(action="create", name="daily", prompt="p", cron="0 9 * * *")
    # Agent re-issues create to change the time — should update, not duplicate.
    out = await tool.execute(action="create", name="daily", prompt="p", cron="0 7 * * *")
    assert "updated it instead" in out.lower()
    rows = await store.list_for_user(user.id)
    assert len(rows) == 1 and rows[0].cron == "0 7 * * *"


async def test_schedule_delete_and_pause_by_name(db_factory, stores):
    store = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("del@x.y")
    tool = ScheduleTool(store, FakeScheduler(), user.id)
    await tool.execute(action="create", name="Nightly", prompt="p", cron="0 2 * * *")

    # Pause via update (enabled=false).
    await tool.execute(action="update", name="Nightly", enabled=False)
    assert (await store.list_for_user(user.id))[0].enabled is False

    # Cancel via delete by NAME (case-insensitive) — the old tool only took an id.
    out = await tool.execute(action="delete", name="nightly")
    assert out.startswith("Deleted task")
    assert await store.list_for_user(user.id) == []


# ---------------------------------------------------------------- document tools

async def test_read_csv(tmp_path):
    (tmp_path / "data.csv").write_text("name,score\nalice,90\nbob,80\n")
    out = await ReadCsvTool(tmp_path).execute(path="data.csv")
    assert "alice | 90" in out and "bob | 80" in out


async def test_read_csv_escape_protection(tmp_path):
    out = await ReadCsvTool(tmp_path).execute(path="../../etc/passwd")
    assert out.startswith("Error: file not found")


def _xlsx(path: Path):
    # Build a minimal .xlsx with openpyxl if available; else skip content check.
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["item", "qty"])
    ws.append(["apple", 5])
    wb.save(path)


async def test_read_excel(tmp_path):
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return  # optional dep not installed — tool returns a clear error, covered elsewhere
    _xlsx(tmp_path / "book.xlsx")
    out = await ReadExcelTool(tmp_path).execute(path="book.xlsx")
    assert "Sheet: Sheet1" in out and "apple | 5" in out


async def test_read_excel_missing_dep_message(tmp_path, monkeypatch):
    # Simulate openpyxl not being importable.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *a, **k)

    (tmp_path / "book.xlsx").write_bytes(b"PK\x03\x04dummy")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = await ReadExcelTool(tmp_path).execute(path="book.xlsx")
    assert "not installed" in out
