"""`spreadsheet` tool (GridCraft): path confinement, command allow-list, flag, and — when the
pinned binary is installed — real engine runs (native and, if Docker is up, in the sandbox)."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claw.config import Settings
from claw.tools import spreadsheet as ss
from claw.tools.spreadsheet import ALLOWED_COMMANDS, SpreadsheetError, SpreadsheetTool, check_steps

NATIVE = SimpleNamespace(enabled=False, timeout_seconds=30)
VENDOR = ss.DEFAULT_VENDOR_DIR.expanduser()


def _tool(ws: Path, sandbox=NATIVE) -> SpreadsheetTool:
    return SpreadsheetTool(ws, sandbox)


def _native_available() -> bool:
    try:
        ss.GridcraftRunner(NATIVE).binary()
        return True
    except SpreadsheetError:
        return False


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        ss.GridcraftRunner(SimpleNamespace(enabled=True)).binary()
        r = subprocess.run(
            ["docker", "image", "inspect", "claw-sandbox:latest"], capture_output=True, timeout=20
        )
        return r.returncode == 0
    except (SpreadsheetError, OSError, subprocess.TimeoutExpired):
        return False


needs_native = pytest.mark.skipif(not _native_available(), reason="gridcraft-cli not installed")
needs_docker = pytest.mark.skipif(
    not os.environ.get("CLAW_TEST_DOCKER") or not _docker_available(),
    reason="set CLAW_TEST_DOCKER=1 with Docker + sandbox image",
)


# ---- flag -------------------------------------------------------------------------------
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("CLAW_SPREADSHEET_ENABLED", raising=False)
    assert Settings(_env_file=None).spreadsheet_enabled is False


# ---- paths ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/etc/hosts.csv",
        "~/secret.xlsx",
        "../other-user/data.xlsx",
        "data/../../x.xlsx",
        "/workspace/../etc/passwd.csv",
        ".env.csv",
        "data/.hidden/x.xlsx",
        "C:/Windows/x.xlsx",
        "file:///etc/passwd",
        "",
    ],
)
async def test_rejects_paths_outside_workspace(tmp_path, bad):
    out = await _tool(tmp_path).execute(action="read", path=bad)
    assert out.startswith("Error:"), out


async def test_etc_passwd_blocked_everywhere(tmp_path):
    t = _tool(tmp_path)
    for kw in (
        {"action": "inspect", "path": "/etc/passwd"},
        {"action": "read", "path": "/etc/passwd"},
        {"action": "evaluate", "formula": "=A1", "path": "/etc/passwd"},
        {"action": "convert", "path": "/etc/passwd", "out": "x.csv"},
        {
            "action": "run",
            "path": "/etc/passwd",
            "steps": [{"command": "cell.get", "params": {"cell": "A1"}}],
        },
        {"action": "create", "values": [["a"]], "out": "/etc/passwd.csv"},
    ):
        out = await t.execute(**kw)
        assert out.startswith("Error:"), (kw, out)


async def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("a,b\n1,2\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link").symlink_to(outside, target_is_directory=True)
    out = await _tool(ws).execute(action="read", path="link/secret.csv")
    assert out.startswith("Error:") and "inside the workspace" in out


async def test_pdf_output_refused(tmp_path):
    (tmp_path / "a.csv").write_text("a\n1\n")
    out = await _tool(tmp_path).execute(action="convert", path="a.csv", out="a.pdf")
    assert out.startswith("Error:") and "pdf" in out.lower()


async def test_unsupported_input_type(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    out = await _tool(tmp_path).execute(action="read", path="a.txt")
    assert out.startswith("Error:") and "not supported" in out


async def test_oversized_input_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "MAX_INPUT_BYTES", 10)
    (tmp_path / "a.csv").write_text("a,b\n1,2\n3,4\n")
    out = await _tool(tmp_path).execute(action="read", path="a.csv")
    assert out.startswith("Error:") and "larger" in out


# ---- command allow-list -------------------------------------------------------------------
@pytest.mark.parametrize(
    "cmd",
    [
        "file.open",
        "file.save",
        "file.saveAs",
        "file.exportPdf",
        "macro.run",
        "automate.runScript",
        "insert.picture",
        "insert.link",
        "link.open",
        "data.fromTextCsv",
        "data.getData",
        "window.close",
        "app.commands",
        "nope.x",
    ],
)
def test_disallowed_commands_blocked(cmd):
    assert cmd not in ALLOWED_COMMANDS
    with pytest.raises(SpreadsheetError, match="not allowed"):
        check_steps([{"command": cmd, "params": {}}])


@pytest.mark.parametrize(
    "params",
    [
        {"path": "x.xlsx"},
        {"range": "A1", "file": "a"},
        {"input": "/etc/passwd"},
        {"input": "~/x"},
        {"values": [["http://169.254.169.254/latest"]]},
        {"nested": {"url": "x"}},
    ],
)
def test_pathlike_params_blocked(params):
    with pytest.raises(SpreadsheetError):
        check_steps([{"command": "cell.set", "params": params}])


def test_formulas_with_slash_allowed():
    steps = check_steps([{"command": "cell.set", "params": {"cell": "C2", "input": "=A2/B2"}}])
    assert steps[0]["params"]["input"] == "=A2/B2"


def test_steps_limits():
    with pytest.raises(SpreadsheetError, match="at most"):
        check_steps([{"command": "cell.get", "params": {}}] * (ss.MAX_STEPS + 1))
    assert (
        check_steps(json.dumps([{"command": "home.bold", "params": {"range": "A1"}}]))[0]["command"]
        == "home.bold"
    )


async def test_create_cell_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "MAX_CELLS", 3)
    out = await _tool(tmp_path).execute(action="create", values=[["a", "b"], [1, 2]])
    assert out.startswith("Error:") and "cells" in out


async def test_disallowed_step_fails_before_running(tmp_path):
    out = await _tool(tmp_path).execute(
        action="create", values=[["a"]], steps=[{"command": "file.save", "params": {}}], out="x.xlsx"
    )
    assert out.startswith("Error:") and "not allowed" in out
    assert not (tmp_path / "x.xlsx").exists()


def test_container_argv_is_confined(tmp_path, monkeypatch):
    runner = ss.GridcraftRunner(
        SimpleNamespace(
            enabled=True, image="claw-sandbox:latest", cpu_limit=1, memory_limit="1g", pids_limit=64
        )
    )
    fake = tmp_path / "gridcraft-cli"
    fake.write_text("")
    monkeypatch.setattr(runner, "binary", lambda: fake)
    argv = runner._argv(["info", "/workspace/a.xlsx"], tmp_path, None, writable=False)
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network none" in joined and "--read-only" in joined and "--cap-drop ALL" in joined
    assert f"source={tmp_path.resolve()},target=/workspace,readonly" in joined
    assert "/bin/sh" not in argv


# ---- real engine ------------------------------------------------------------------------
async def _e2e(tool: SpreadsheetTool, ws: Path):
    out = await tool.execute(
        action="create",
        values=[["ภาค", "ยอดขาย"], ["เหนือ", 100], ["ใต้", 250.5], ["กลาง", 175]],
        sheet_name="สรุป",
        steps=[
            {"command": "cell.set", "params": {"cell": "A5", "input": "รวม"}},
            {"command": "cell.set", "params": {"cell": "B5", "input": "=SUM(B2:B4)"}},
            {
                "command": "insert.chart",
                "params": {"range": "A1:B4", "type": "column", "title": "ยอดขาย", "at": "D2"},
            },
        ],
        out="sheets/sales.xlsx",
    )
    assert "Error" not in out, out
    assert "รวม,525.5" in out
    import openpyxl

    wb = openpyxl.load_workbook(ws / "sheets/sales.xlsx")
    assert wb.sheetnames == ["สรุป"] and wb.active["B5"].value == "=SUM(B2:B4)"
    assert openpyxl.load_workbook(ws / "sheets/sales.xlsx", data_only=True).active["B5"].value == 525.5

    (ws / "data").mkdir(exist_ok=True)
    (ws / "data/costs.csv").write_text("item,cost\nRent,1200\nFood,400.25\n", encoding="utf-8")
    out = await tool.execute(action="convert", path="data/costs.csv", out="sheets/costs.xlsx")
    assert "Error" not in out and "Food,400.25" in out
    out = await tool.execute(action="evaluate", path="sheets/costs.xlsx", formula="=SUM(B2:B3)")
    assert "1600.25" in out
    out = await tool.execute(action="read", path="sheets/sales.xlsx", range="A1:B5")
    assert "เหนือ,100" in out and "525.5" in out
    out = await tool.execute(action="inspect", path="sheets/sales.xlsx")
    assert "สรุป" in out


@needs_native
async def test_engine_native(tmp_path):
    await _e2e(_tool(tmp_path), tmp_path)


@needs_docker
async def test_engine_in_sandbox(tmp_path):
    sandbox = SimpleNamespace(
        enabled=True,
        image="claw-sandbox:latest",
        cpu_limit=1,
        memory_limit="512m",
        pids_limit=64,
        timeout_seconds=60,
    )
    await _e2e(_tool(tmp_path, sandbox), tmp_path)


def test_unknown_command_suggests_allowed_one():
    with pytest.raises(SpreadsheetError, match="data.filter"):
        check_steps([{"command": "insert.autofilter", "params": {}}])


def test_xlsx_skill_points_to_tool_only_when_enabled():
    from claw.core.builtin_skills import get_builtin_skill

    try:
        ss.configure(False)
        assert "spreadsheet` tool" not in get_builtin_skill("xlsx").content
        ss.configure(True)
        skill = get_builtin_skill("xlsx")
        assert skill.content.startswith(ss.XLSX_SKILL_NOTE) and skill.description.startswith(
            "Use the `spreadsheet` tool"
        )
    finally:
        ss.configure(False)


def test_file_jobs_keep_the_spreadsheet_tool_in_scope():
    from claw.core.runtime import _artifact_tool_scope

    scope = _artifact_tool_scope(["spreadsheet", "exec"], "สร้างไฟล์ Excel สรุปยอด", [], set())
    assert "spreadsheet" in scope


async def test_evaluate_with_cell_refs_needs_a_file(tmp_path):
    out = await _tool(tmp_path).execute(action="evaluate", formula="=SUM(B2:B9)")
    assert out.startswith("Error:") and "path" in out


@needs_native
async def test_run_without_out_edits_in_place_and_hints_thai_sheet_quotes(tmp_path):
    t = _tool(tmp_path)
    (tmp_path / "a.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    out = await t.execute(
        action="run",
        path="a.csv",
        steps=[{"command": "cell.set", "params": {"cell": "C2", "input": "=A2+B2"}}],
    )
    assert "Saved a.xlsx" in out and "1,2,3" in out
    out = await t.execute(
        action="create",
        values=[["a"], [1]],
        sheet_name="ข้อมูล",
        out="t.xlsx",
        steps=[{"command": "cell.set", "params": {"cell": "B1", "input": "=SUM(ข้อมูล!A2)"}}],
    )
    if out.startswith("Error:"):
        assert "single quotes" in out
    out = await t.execute(
        action="create",
        values=[["a"], [1]],
        sheet_name="ข้อมูล",
        out="t.xlsx",
        steps=[{"command": "cell.set", "params": {"cell": "B1", "input": "=SUM('ข้อมูล'!A2)"}}],
    )
    assert "Error" not in out and "a,1" in out


async def test_run_without_path_does_not_clobber_existing_file(tmp_path):
    (tmp_path / "keep.xlsx").write_bytes(b"PK-not-really")
    t = _tool(tmp_path)
    out = await t.execute(
        action="run", out="keep.xlsx", steps=[{"command": "cell.set", "params": {"cell": "A1", "input": "x"}}]
    )
    assert out.startswith("Error:") and "BLANK" in out
    out = await t.execute(action="create", values=[["a"]], out="keep.xlsx")
    assert out.startswith("Error:") and "already exists" in out
    assert (tmp_path / "keep.xlsx").read_bytes() == b"PK-not-really"


@needs_native
async def test_pivot_from_csv_matches_python(tmp_path):
    import csv
    from collections import defaultdict

    rows = [["dept", "amt"], ["A", 10.5], ["B", 20], ["A", 5.25], ["C", 1]]
    (tmp_path / "d.csv").write_text("\n".join(",".join(map(str, r)) for r in rows) + "\n")
    out = await _tool(tmp_path).execute(
        action="run",
        path="d.csv",
        out="p.xlsx",
        steps=[
            {
                "command": "insert.pivotTable",
                "params": {"source": "A1:B5", "destination": "new", "rows": ["dept"], "values": ["amt"]},
            }
        ],
    )
    want = defaultdict(float)
    for r in csv.DictReader((tmp_path / "d.csv").open()):
        want[r["dept"]] += float(r["amt"])
    assert "A,15.75" in out and "B,20" in out and "Grand Total,36.75" in out and sum(want.values()) == 36.75


async def test_convert_does_not_replace_existing_user_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/s.csv").write_text("a\n1\n")
    (tmp_path / "data/s.xlsx").write_bytes(b"original")
    out = await _tool(tmp_path).execute(action="convert", path="data/s.csv", out="data/s.xlsx")
    assert out.startswith("Error:") and "s-converted.xlsx" in out
    assert (tmp_path / "data/s.xlsx").read_bytes() == b"original"
    out = await _tool(tmp_path).execute(
        action="run",
        path="data/s.csv",
        out="data/s.xlsx",
        steps=[{"command": "cell.get", "params": {"cell": "A1"}}],
    )
    assert out.startswith("Error:") and "not the file being edited" in out



def test_number_format_steps_for_create():
    steps = ss._number_format_steps(
        [["region", "amount", "orders", "note"], ["N", 1.5, 3, "x"], ["S", 2, 4, None], ["E", 3.25, True, "y"]]
    )
    assert steps == [
        {"command": "home.numberFormat", "params": {"range": "B2:B4", "code": "#,##0.00"}},
        {"command": "home.numberFormat", "params": {"range": "C2:C4", "code": "#,##0"}},
    ]
    assert ss._column_letter(0) == "A" and ss._column_letter(25) == "Z" and ss._column_letter(27) == "AB"


@needs_native
async def test_create_writes_thousands_number_formats(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    out = await _tool(tmp_path).execute(
        action="create",
        out="f.xlsx",
        values=[["region", "amount", "orders"], ["N", 3647010.889999999, 1200], ["S", 12.5, 3]],
    )
    assert not out.startswith("Error:"), out
    ws = openpyxl.load_workbook(tmp_path / "f.xlsx").worksheets[0]
    assert ws["B2"].number_format == "#,##0.00" and ws["C2"].number_format == "#,##0"
    assert ws["A1"].value == "region" and ws["B1"].number_format == "General"
