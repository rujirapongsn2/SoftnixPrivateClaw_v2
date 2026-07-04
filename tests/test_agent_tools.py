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
    assert out == "Deleted." and sched.notified == 2


async def test_schedule_tool_validates(db_factory, stores):
    tool = ScheduleTool(ScheduleStore(db_factory), FakeScheduler(), "u1")
    assert (await tool.execute(action="create", name="x")).startswith("Error: create requires a prompt")
    assert (await tool.execute(action="create", prompt="p", cron="not a cron")).startswith("Error:")
    assert (await tool.execute(action="delete")).startswith("Error: delete requires")


async def test_schedule_tool_interval(db_factory, stores):
    store = ScheduleStore(db_factory)
    user = await stores["users"].get_or_create_by_email("s2@x.y")
    tool = ScheduleTool(store, FakeScheduler(), user.id)
    await tool.execute(action="create", name="hourly", prompt="check", interval_minutes=60)
    rows = await store.list_for_user(user.id)
    assert rows[0].interval_seconds == 3600


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
