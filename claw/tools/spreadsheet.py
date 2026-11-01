"""`spreadsheet` tool (behind CLAW_SPREADSHEET_ENABLED): create, edit, convert and read
workbooks with the GridCraft engine (`gridcraft-cli`, pinned version).

Spec: docs/spreadsheet-gridcraft-spec.md. Each call is one `gridcraft-cli` process. With the
sandbox enabled it runs in a throwaway `claw-sandbox` container (Linux binary mounted read-only,
no network, only this user's workspace mounted); otherwise the native binary runs as a
subprocess with cwd = workspace. Paths are confined to the workspace and engine commands go
through an allow-list, so the model cannot reach host files through GridCraft.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import platform
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from claw.sandbox.ephemeral import sandbox_user
from claw.tools.base import Tool

GRIDCRAFT_VERSION = "0.14.2"
DEFAULT_VENDOR_DIR = Path("~/.softnix/claw/vendor/gridcraft") / GRIDCRAFT_VERSION

INPUT_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls", ".xlsb", ".ods", ".csv", ".tsv"})
OUTPUT_SUFFIXES = frozenset({".xlsx", ".csv", ".tsv", ".html"})
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_CELLS = 50_000
MAX_STEPS = 300
MAX_TIMEOUT_S = 60
OUTPUT_CAP = 20_000
PRINT_ROWS = 40
_CONCURRENCY = asyncio.Semaphore(4)

# Mirrors Settings.spreadsheet_enabled for code without the settings object (the built-in skill
# list); set once by claw.main.create_app.
_enabled = False


def configure(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    return _enabled


PIVOT_RECIPE = (
    "Totals per group from a data file (e.g. expense per department) = ONE call: action=run, path=<the csv/xlsx>, "
    'steps=[{"command":"insert.pivotTable","params":{"source":"A1:D109","destination":"new","rows":["department"],'
    '"values":["expense_thb"]}}, {"command":"insert.chart","params":{"range":"A3:B9","type":"column","title":"...",'
    '"at":"D3"}}], out=<name>.xlsx. The pivot report starts at A3 on a new first sheet: a header row, one row per group, '
    "then Grand Total (get the data range from inspect first). Do not pre-aggregate with exec and do not type the numbers."
)

XLSX_SKILL_NOTE = (
    "> **The `spreadsheet` tool is available on this server — use it first.** Create, convert, edit and read "
    ".xlsx/.csv files with `spreadsheet` (actions create/convert/run/read/evaluate/inspect): the engine writes real "
    "formulas and charts and computes the totals you quote. "
    + PIVOT_RECIPE
    + " Only fall back to the openpyxl/pandas recipes below for something the tool cannot do (e.g. a PDF, or styling "
    "it does not offer), and say why.\n\n"
)

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_BIN = "/opt/gridcraft/gridcraft-cli"
CONTAINER_JOB = "/job"

# Engine commands the model may run. Everything that touches files, windows, macros/scripts,
# links, pictures, external data, printing or the app is deliberately absent.
ALLOWED_COMMANDS = frozenset(
    {
        # values
        "cell.set",
        "cell.get",
        "range.setValues",
        "range.fill",
        "edit.fillDown",
        "edit.fillRight",
        "edit.fillUp",
        "edit.fillLeft",
        "edit.autoFill",
        "edit.fillSeries",
        "edit.clearAll",
        "edit.clearFormats",
        "edit.clearContents",
        "edit.replace",
        "edit.flashFill",
        "selection.set",
        # formatting
        "home.bold",
        "home.italic",
        "home.underline",
        "home.doubleUnderline",
        "home.strikethrough",
        "home.fontName",
        "home.fontSize",
        "home.increaseFontSize",
        "home.decreaseFontSize",
        "home.fontColor",
        "home.fillColor",
        "home.borders",
        "home.formatCells",
        "home.alignLeft",
        "home.alignCenter",
        "home.alignRight",
        "home.justify",
        "home.alignTop",
        "home.alignMiddle",
        "home.alignBottom",
        "home.wrapText",
        "home.shrinkToFit",
        "home.increaseIndent",
        "home.decreaseIndent",
        "home.orientation",
        "home.mergeCenter",
        "home.mergeAcross",
        "home.mergeCells",
        "home.unmergeCells",
        "home.numberFormat",
        "home.accounting",
        "home.percent",
        "home.comma",
        "home.increaseDecimal",
        "home.decreaseDecimal",
        "home.cellStyle",
        "home.rowHeight",
        "home.columnWidth",
        "home.autofitRowHeight",
        "home.autofitColumnWidth",
        "home.hideRows",
        "home.hideColumns",
        "home.unhideRows",
        "home.unhideColumns",
        "home.insertRows",
        "home.insertColumns",
        "home.deleteRows",
        "home.deleteColumns",
        "home.insertCells",
        "home.deleteCells",
        "home.conditionalFormat",
        "home.clearRules",
        # sheets
        "home.insertSheet",
        "home.deleteSheet",
        "sheet.rename",
        "sheet.move",
        "sheet.tabColor",
        "sheet.activate",
        "sheet.moveColumns",
        # tables
        "insert.table",
        "home.formatAsTable",
        "table.totalRow",
        "table.bandedRows",
        "table.bandedColumns",
        "table.headerRow",
        "table.firstColumn",
        "table.lastColumn",
        "table.style",
        "table.rename",
        "table.convertToRange",
        "table.totalFunction",
        "table.resize",
        "table.removeDuplicates",
        # charts
        "insert.chart",
        "chart.set",
        "chart.switchRowColumn",
        "chart.changeType",
        "chart.addElement",
        "chart.quickLayout",
        "chart.changeColors",
        "chart.delete",
        "insert.sparkline",
        # data
        "data.sort",
        "data.sortAscending",
        "data.sortDescending",
        "data.filter",
        "data.filterBy",
        "data.clearFilter",
        "data.reapply",
        "data.removeDuplicates",
        "data.textToColumns",
        "data.validation",
        "data.subtotal",
        "data.group",
        "data.ungroup",
        # formulas / names
        "formulas.autoSum",
        "formulas.defineName",
        "formulas.deleteName",
        "formulas.calculateNow",
        "formulas.convertToNumber",
        # pivot tables
        "insert.pivotTable",
        "pivot.addField",
        "pivot.removeField",
        "pivot.moveField",
        "pivot.valueSettings",
        "pivot.filter",
        "pivot.sort",
        "pivot.group",
        "pivot.layout",
        "pivot.grandTotals",
        "pivot.subtotals",
        "pivot.reportLayout",
        "pivot.style",
        "pivot.refresh",
        "pivot.delete",
        # view / notes
        "view.freezePanes",
        "view.freezeTopRow",
        "view.freezeFirstColumn",
        "view.unfreezePanes",
        "review.newNote",
        "pageLayout.orientation",
        # read-only
        "document.inspect",
        "sheet.read",
        "selection.stats",
    }
)
_DENIED_PARAM_KEYS = frozenset(
    {"path", "file", "filename", "filepath", "dir", "folder", "url", "href", "uri"}
)
_CELL_REF = re.compile(
    r"(?<![A-Za-z_])\$?[A-Za-z]{1,3}\$?[0-9]+\b(?!\s*\()|![A-Za-z$]|\b[A-Za-z]{1,3}:[A-Za-z]{1,3}\b"
)
_PATHLIKE = re.compile(r"^(?:/|~|\\\\|[a-zA-Z]:[\\/]|file:|[a-zA-Z][a-zA-Z0-9+.-]*://)")


class SpreadsheetError(Exception):
    """A problem the model should see and fix."""


@dataclass(slots=True)
class RunResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _arch() -> str:
    m = platform.machine().lower()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)


def check_steps(steps: Any) -> list[dict]:
    """Validate engine steps against the allow-list; return them normalized."""
    if steps in (None, ""):
        return []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError as exc:
            raise SpreadsheetError(f"steps: not valid JSON ({exc.msg})") from exc
    if not isinstance(steps, list):
        raise SpreadsheetError("steps: a list of {command, params}")
    if len(steps) > MAX_STEPS:
        raise SpreadsheetError(f"steps: at most {MAX_STEPS}")
    out = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise SpreadsheetError(f"steps[{i}]: an object {{command, params}}")
        cmd = step.get("command") or step.get("id")
        if not isinstance(cmd, str) or not cmd:
            raise SpreadsheetError(f"steps[{i}].command: required")
        if cmd not in ALLOWED_COMMANDS:
            close = difflib.get_close_matches(cmd, sorted(ALLOWED_COMMANDS), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise SpreadsheetError(
                f"steps[{i}].command `{cmd}` is not allowed (files, macros, links, pictures, external data and "
                f"export are handled by the tool itself).{hint} AutoFilter = data.filter."
            )
        params = step.get("params") or {}
        if not isinstance(params, dict):
            raise SpreadsheetError(f"steps[{i}].params: an object")
        _scan_params(params, f"steps[{i}].params")
        out.append({"command": cmd, "params": params})
    return out


def _scan_params(value: Any, where: str, depth: int = 0) -> None:
    if depth > 8:
        raise SpreadsheetError(f"{where}: nested too deeply")
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in _DENIED_PARAM_KEYS:
                raise SpreadsheetError(f"{where}.{k}: file/URL parameters are not allowed")
            _scan_params(v, f"{where}.{k}", depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_params(v, f"{where}[{i}]", depth + 1)
    elif isinstance(value, str) and not value.startswith("=") and _PATHLIKE.match(value.strip()):
        raise SpreadsheetError(f"{where}: paths and URLs are not allowed in engine parameters")


class GridcraftRunner:
    """Runs one gridcraft-cli invocation, in the sandbox container or natively."""

    def __init__(self, sandbox_settings: Any, vendor_dir: Path | None = None):
        self.sandbox = sandbox_settings
        self.vendor_dir = (vendor_dir or DEFAULT_VENDOR_DIR).expanduser()
        self._version_ok: bool | None = None

    @property
    def in_container(self) -> bool:
        return bool(getattr(self.sandbox, "enabled", False))

    def binary(self) -> Path:
        if self.in_container:
            p = self.vendor_dir / f"linux-{_arch()}" / "gridcraft-cli"
        elif platform.system() == "Darwin":
            p = self.vendor_dir / "macos" / "gridcraft-cli"
        else:
            p = self.vendor_dir / f"{platform.system().lower()}-{_arch()}" / "gridcraft-cli"
        if not p.is_file():
            raise SpreadsheetError(
                f"GridCraft {GRIDCRAFT_VERSION} is not installed on this server (run scripts/install-gridcraft.sh)"
            )
        return p

    def workspace_arg(self, workspace: Path, rel: str) -> str:
        return f"{CONTAINER_WORKSPACE}/{rel}" if self.in_container else str((workspace / rel).resolve())

    def job_arg(self, job_dir: Path, name: str) -> str:
        return f"{CONTAINER_JOB}/{name}" if self.in_container else str(job_dir / name)

    def _argv(self, args: list[str], workspace: Path, job_dir: Path | None, writable: bool) -> list[str]:
        binary = self.binary()
        if not self.in_container:
            return [str(binary), *args]
        s = self.sandbox
        argv = [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:size=64m",
            "--cpus",
            str(getattr(s, "cpu_limit", 1.0)),
            "--memory",
            str(getattr(s, "memory_limit", "1g")),
            "--pids-limit",
            str(getattr(s, "pids_limit", 256)),
            # Same uid as exec runs, so files GridCraft writes stay editable by them.
            *(["--user", user] if (user := sandbox_user(getattr(s, "user", "auto"), workspace)) else []),
            "-e",
            "GRIDCRAFT_NO_PREFS=1",
            "-e",
            "HOME=/tmp",
            "--workdir",
            CONTAINER_WORKSPACE,
            "--mount",
            f"type=bind,source={binary.resolve()},target={CONTAINER_BIN},readonly",
            "--mount",
            f"type=bind,source={workspace.resolve()},target={CONTAINER_WORKSPACE}"
            + ("" if writable else ",readonly"),
        ]
        if job_dir is not None:
            argv += ["--mount", f"type=bind,source={job_dir.resolve()},target={CONTAINER_JOB},readonly"]
        argv += ["--entrypoint", CONTAINER_BIN, str(getattr(s, "image", "claw-sandbox:latest")), *args]
        return argv

    def timeout(self) -> int:
        return max(
            5,
            min(int(getattr(self.sandbox, "timeout_seconds", MAX_TIMEOUT_S) or MAX_TIMEOUT_S), MAX_TIMEOUT_S),
        )

    async def run(
        self, args: list[str], workspace: Path, job_dir: Path | None = None, writable: bool = False
    ) -> RunResult:
        await self._check_version(workspace)
        return await self._exec(args, workspace, job_dir, writable)

    async def _exec(
        self, args: list[str], workspace: Path, job_dir: Path | None, writable: bool
    ) -> RunResult:
        argv = self._argv(args, workspace, job_dir, writable)
        env = None
        if not self.in_container:
            env = {
                "PATH": "/usr/bin:/bin",
                "GRIDCRAFT_NO_PREFS": "1",
                "HOME": tempfile.gettempdir(),
                "LANG": "C.UTF-8",
            }
        else:
            env = {
                k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
            }
        async with _CONCURRENCY:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=None if self.in_container else str(workspace),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise SpreadsheetError(f"could not start GridCraft ({exc})") from exc
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout())
            except asyncio.TimeoutError:
                proc.kill()
                out, err = await proc.communicate()
                return RunResult(-1, out.decode("utf-8", "replace")[:OUTPUT_CAP], "timed out", True)
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.kill()
                await proc.communicate()
                raise
        return RunResult(
            proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", "replace")[:OUTPUT_CAP],
            err.decode("utf-8", "replace")[-4000:],
        )

    async def _check_version(self, workspace: Path) -> None:
        if self._version_ok:
            return
        res = await self._exec(["version"], workspace, None, False)
        got = res.stdout.strip()
        self._version_ok = res.code == 0 and got == f"gridcraft-cli {GRIDCRAFT_VERSION}"
        if not self._version_ok:
            logger.warning(
                "GridCraft version check failed: code={} out={!r} err={!r}", res.code, got, res.stderr[-300:]
            )
            self._version_ok = None
            raise SpreadsheetError(
                f"GridCraft {GRIDCRAFT_VERSION} is not available ({got or res.stderr.strip()[:200]})"
            )


_DESCRIPTION = (
    "USE THIS FIRST for any Excel/.xlsx/spreadsheet/CSV task — making a workbook, converting csv<->xlsx, adding totals, "
    "charts or tables to a file, or totals/counts from a spreadsheet file (action evaluate with =SUM/=SUMIF/=COUNTIF) — "
    "instead of exec with openpyxl/pandas. "
    "Create, edit, convert and read spreadsheet files (.xlsx/.csv/…) with a real spreadsheet engine (GridCraft): native "
    "formulas (=SUM, =AVERAGE, 500+ functions), charts, tables, sort/filter, pivot tables. The engine computes every "
    "number, so quote totals exactly as this tool returns them. Prefer this tool over writing openpyxl code when the user "
    "wants a spreadsheet file. Paths are relative to the workspace (e.g. 'data/sales.csv').\n"
    "Actions: inspect(path) sheets/ranges; read(path, range?, sheet?) values as CSV; evaluate(formula, path, sheet?) "
    "e.g. '=SUM(B2:B100)' on a file without changing it; convert(path, out) between xlsx/xlsm/xls/xlsb/ods/csv/tsv -> "
    "xlsx/csv/tsv/html; create(values=2-D rows with a header row, steps?, out?, sheet_name?) a new workbook with the "
    "values at A1; run(path?, steps, out?) open a file (or blank), run engine steps and save (no out = save over path as .xlsx).\n"
    'steps = [{command, params}] e.g. {"command":"cell.set","params":{"cell":"B10","input":"=SUM(B2:B9)"}}, '
    '{"command":"insert.chart","params":{"range":"A1:B9","type":"column","title":"...","at":"D2"}} '
    "(types column|bar|line|pie|doughnut|area|scatter|combo|waterfall|funnel…), "
    '{"command":"home.bold","params":{"range":"A1:B1"}}, '
    '{"command":"home.numberFormat","params":{"range":"B2:B10","code":"#,##0.00"}}, '
    '{"command":"insert.table","params":{"range":"A1:B9"}}, '
    '{"command":"data.sort","params":{"range":"A1:B9","header":true,"keys":[{"column":"B","order":"desc"}]}}, '
    '{"command":"insert.pivotTable","params":{"source":"Sheet1!A1:C50","rows":["region"],"values":["amount"]}}, '
    '{"command":"home.autofitColumnWidth","params":{}}. Formulas use English function names with \',\' separators. '
    "File/macro/link/picture/export commands are not allowed. PDF output is not supported (Thai text breaks): make the "
    "xlsx/csv here, then follow the 'pdf' skill for a PDF. The saved file is shown to the user as a downloadable "
    "artifact; for a chart inside the chat use chart_from_data on the same data. Name sheets, headers and chart titles "
    "in the language of the user's latest message, and reply in that language. To summarise a big table (totals per "
    "group) use insert.pivotTable or =SUMIF formulas in steps so the engine computes them, not numbers you typed. "
    + PIVOT_RECIPE
)



def _column_letter(index: int) -> str:
    """0-based column index -> A, B, ..., Z, AA, ..."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _number_format_steps(values: list[list]) -> list[dict]:
    """Thousands separators for the numeric columns `create` writes: "#,##0.00" when
    a column holds any fraction (money, averages), "#,##0" for whole numbers. Header
    row is skipped; the model's own steps run after these and may override them."""
    body = values[1:]
    width = max((len(r) for r in body), default=0)
    steps = []
    for col in range(width):
        nums = [
            r[col] for r in body
            if col < len(r) and isinstance(r[col], (int, float)) and not isinstance(r[col], bool)
        ]
        if not nums:
            continue
        fmt = "#,##0.00" if any(isinstance(n, float) and not n.is_integer() for n in nums) else "#,##0"
        letter = _column_letter(col)
        steps.append({
            "command": "home.numberFormat",
            "params": {"range": f"{letter}2:{letter}{len(values)}", "code": fmt},
        })
    return steps

class SpreadsheetTool(Tool):
    name = "spreadsheet"
    description = _DESCRIPTION
    writes_workspace = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "read", "evaluate", "convert", "create", "run"]},
            "path": {
                "type": "string",
                "description": "Workspace path of the input file (inspect/read/evaluate/convert/run).",
            },
            "out": {
                "type": "string",
                "description": "Workspace path to save (.xlsx/.csv/.tsv/.html). Default for create/run: sheets/<name>-<time>.xlsx.",
            },
            "range": {"type": "string", "description": "read: e.g. 'A1:F20' (default: used range)."},
            "sheet": {"type": "string", "description": "Sheet name (default: first/active)."},
            "formulas": {"type": "boolean", "description": "read: show formulas instead of values."},
            "formula": {"type": "string", "description": "evaluate: e.g. '=SUM(B2:B20)'."},
            "values": {
                "type": "array",
                "items": {"type": "array"},
                "description": "create: rows of cells (numbers/strings/bools/null), first row = header. Strings starting with '=' are formulas.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing `out` file that is not the file being edited (only when the user asked).",
            },
            "sheet_name": {
                "type": "string",
                "maxLength": 31,
                "description": "create: name of the first sheet.",
            },
            "name": {
                "type": "string",
                "maxLength": 60,
                "description": "Short slug for the default output file name.",
            },
            "steps": {
                "description": "Engine commands [{command, params}] run in order (create/run).",
                "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "string"}],
            },
        },
        "required": ["action"],
    }

    def __init__(self, workspace: Path, sandbox_settings: Any, vendor_dir: Path | None = None):
        self.workspace = workspace
        self.runner = GridcraftRunner(sandbox_settings, vendor_dir)

    # ---- paths -------------------------------------------------------------------------
    def _rel(self, raw: Any, *, field: str, suffixes: frozenset[str], must_exist: bool) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise SpreadsheetError(f"{field}: required (a workspace path, e.g. 'data/sales.xlsx')")
        s = raw.strip().replace("\\", "/")
        if any(ch in s for ch in "\x00\n\r"):
            raise SpreadsheetError(f"{field}: invalid characters")
        if s.startswith(CONTAINER_WORKSPACE + "/"):
            s = s[len(CONTAINER_WORKSPACE) + 1 :]
        if s.startswith(("/", "~")) or re.match(r"^[a-zA-Z]:", s) or "://" in s:
            raise SpreadsheetError(f"{field}: must be a path inside the workspace, not an absolute path")
        parts = [p for p in s.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise SpreadsheetError(f"{field}: must stay inside the workspace")
        if any(p.startswith(".") for p in parts):
            raise SpreadsheetError(f"{field}: hidden files/folders are not allowed")
        root = self.workspace.resolve()
        target = (root / "/".join(parts)).resolve()
        if not target.is_relative_to(root):
            raise SpreadsheetError(f"{field}: must stay inside the workspace")
        suffix = target.suffix.lower()
        if suffix == ".pdf":
            raise SpreadsheetError(
                "PDF output is not supported (GridCraft cannot render Thai in PDF). Save .xlsx/.csv, then use the 'pdf' skill."
            )
        if suffix not in suffixes:
            raise SpreadsheetError(
                f"{field}: file type {suffix or '(none)'} not supported; use one of {sorted(suffixes)}"
            )
        if must_exist:
            if not target.is_file():
                raise SpreadsheetError(f"{field}: file not found: {raw}")
            if target.stat().st_size > MAX_INPUT_BYTES:
                raise SpreadsheetError(f"{field}: file is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB")
        else:
            if target.exists() and not target.is_file():
                raise SpreadsheetError(f"{field}: is a folder")
            target.parent.mkdir(parents=True, exist_ok=True)
        return target.relative_to(root).as_posix()

    def _default_out(self, name: Any) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", str(name or "sheet").lower()).strip("-")[:40] or "sheet"
        return f"sheets/{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"

    # ---- actions -----------------------------------------------------------------------
    async def execute(self, **kw: Any) -> str:
        action = kw.get("action")
        try:
            handler = {
                "inspect": self._inspect,
                "read": self._read,
                "evaluate": self._evaluate,
                "convert": self._convert,
                "create": self._create,
                "run": self._run,
            }.get(action)
            if handler is None:
                raise SpreadsheetError("action: one of inspect, read, evaluate, convert, create, run")
            return await handler(kw)
        except SpreadsheetError as exc:
            return f"Error: {exc}"

    def _check(self, res: RunResult, what: str) -> str:
        if res.timed_out:
            raise SpreadsheetError(
                f"{what} timed out after {self.runner.timeout()} s; use a smaller file or fewer steps"
            )
        if res.code != 0:
            msg = (res.stderr.strip() or res.stdout.strip())[-800:]
            msg = msg.replace(CONTAINER_WORKSPACE + "/", "")
            if "problem with this formula" in msg and re.search(r"[^\x00-\x7f]", msg):
                msg += (
                    " — put sheet names that are not plain English letters in single quotes, e.g. "
                    "='ข้อมูล'!B2 or =SUMIF('ข้อมูล'!B:B,A4,'ข้อมูล'!D:D)"
                )
            if "PivotTable" in msg or "reference isn't valid" in msg:
                msg += (
                    " — a pivot table needs the data open: pass path=<the csv/xlsx with a header row> and "
                    "source='A1:<last col><last row>' (see inspect)"
                )
            raise SpreadsheetError(f"{what} failed: {msg}")
        return res.stdout

    async def _inspect(self, kw: dict) -> str:
        rel = self._rel(kw.get("path"), field="path", suffixes=INPUT_SUFFIXES, must_exist=True)
        out = self._check(
            await self.runner.run(
                ["info", self.runner.workspace_arg(self.workspace, rel), "--json"], self.workspace
            ),
            "inspect",
        )
        return out.replace(CONTAINER_WORKSPACE + "/", "")

    async def _read(self, kw: dict) -> str:
        rel = self._rel(kw.get("path"), field="path", suffixes=INPUT_SUFFIXES, must_exist=True)
        args = ["cat", self.runner.workspace_arg(self.workspace, rel), "--csv"]
        for key, flag in (("range", "--range"), ("sheet", "--sheet")):
            if kw.get(key):
                args += [flag, str(kw[key])]
        if kw.get("formulas"):
            args.append("--formulas")
        out = self._check(await self.runner.run(args, self.workspace), "read")
        note = "\n[output truncated: read a smaller range]" if len(out) >= OUTPUT_CAP else ""
        return f"{rel} (CSV of displayed values):\n{out}{note}"

    async def _evaluate(self, kw: dict) -> str:
        formula = kw.get("formula")
        if not isinstance(formula, str) or not formula.strip():
            raise SpreadsheetError("formula: required, e.g. '=SUM(B2:B20)'")
        f = formula.strip()
        if not f.startswith("="):
            f = "=" + f
        args = ["eval", f, "--json"]
        if not kw.get("path") and _CELL_REF.search(f):
            raise SpreadsheetError(
                "path: required when the formula refers to cells (e.g. path='data/sales.xlsx')"
            )
        if kw.get("path"):
            rel = self._rel(kw.get("path"), field="path", suffixes=INPUT_SUFFIXES, must_exist=True)
            args += ["--in", self.runner.workspace_arg(self.workspace, rel)]
        if kw.get("sheet"):
            args += ["--sheet", str(kw["sheet"])]
        return self._check(await self.runner.run(args, self.workspace), "evaluate").strip()

    async def _convert(self, kw: dict) -> str:
        src = self._rel(kw.get("path"), field="path", suffixes=INPUT_SUFFIXES, must_exist=True)
        dst = self._rel(kw.get("out"), field="out", suffixes=OUTPUT_SUFFIXES, must_exist=False)
        if (self.workspace.resolve() / dst).is_file() and not kw.get("overwrite"):
            alt = Path(dst).with_name(Path(dst).stem + "-converted" + Path(dst).suffix).as_posix()
            raise SpreadsheetError(
                f"out `{dst}` already exists (it may be the user's own file). Pick a new name, e.g. '{alt}', "
                "or pass overwrite=true only if the user asked to replace it."
            )
        args = [
            "convert",
            self.runner.workspace_arg(self.workspace, src),
            self.runner.workspace_arg(self.workspace, dst),
        ]
        if kw.get("sheet"):
            args += ["--sheet", str(kw["sheet"])]
        self._check(await self.runner.run(args, self.workspace, writable=True), "convert")
        return await self._saved(dst, f"Converted {src} -> {dst}.")

    async def _create(self, kw: dict) -> str:
        values = kw.get("values")
        if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
            raise SpreadsheetError("values: required, a list of rows (lists), first row = header")
        cells = sum(len(r) for r in values)
        if cells > MAX_CELLS:
            raise SpreadsheetError(
                f"values: at most {MAX_CELLS} cells; save the data as .csv and use convert/run"
            )
        for r in values:
            for v in r:
                if v is not None and not isinstance(v, (str, int, float, bool)):
                    raise SpreadsheetError("values: cells must be numbers, strings, booleans or null")
        steps = [{"command": "range.setValues", "params": {"range": "A1", "values": values}}]
        steps += _number_format_steps(values)
        if kw.get("sheet_name"):
            steps.append({"command": "sheet.rename", "params": {"name": str(kw["sheet_name"])[:31]}})
        steps += check_steps(kw.get("steps"))
        dst = self._rel(
            kw.get("out") or self._default_out(kw.get("name")),
            field="out",
            suffixes=OUTPUT_SUFFIXES,
            must_exist=False,
        )
        if (self.workspace.resolve() / dst).is_file() and not kw.get("overwrite"):
            raise SpreadsheetError(
                f"out `{dst}` already exists; create makes a NEW workbook. Use run with path='{dst}' "
                "to edit it, another out name, or overwrite=true to replace it."
            )
        return await self._script(None, steps, dst)

    async def _run(self, kw: dict) -> str:
        steps = check_steps(kw.get("steps"))
        if not steps:
            raise SpreadsheetError("steps: required for run (use convert to only change the format)")
        src = None
        if kw.get("path"):
            src = self._rel(kw.get("path"), field="path", suffixes=INPUT_SUFFIXES, must_exist=True)
        out = kw.get("out")
        if not out and src:
            # Editing a file: save next to it (in place for .xlsx, same name as .xlsx for csv/ods/...).
            out = str(Path(src).with_suffix(".xlsx"))
        dst = self._rel(
            out or self._default_out(kw.get("name")),
            field="out",
            suffixes=OUTPUT_SUFFIXES,
            must_exist=False,
        )
        if src and dst != src and (self.workspace.resolve() / dst).is_file() and not kw.get("overwrite"):
            raise SpreadsheetError(
                f"out `{dst}` already exists and is not the file being edited ({src}). Pick a new name, or pass "
                "overwrite=true only if the user asked to replace it."
            )
        if not src and (self.workspace.resolve() / dst).is_file() and not kw.get("overwrite"):
            raise SpreadsheetError(
                f"out `{dst}` already exists and no `path` was given, so run would start from a BLANK workbook and "
                f"replace it. To edit that file pass path='{dst}'; to really replace it pass overwrite=true."
            )
        return await self._script(src, steps, dst)

    async def _script(self, src: str | None, steps: list[dict], dst: str) -> str:
        job = Path(tempfile.mkdtemp(prefix="claw-gridcraft-"))
        try:
            (job / "steps.jsonl").write_text(
                "\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n", encoding="utf-8"
            )
            if os.getuid() == 0:
                # A root server runs GridCraft as the workspace owner (sandbox_user),
                # who must be able to read the root-owned 0700 job dir.
                job.chmod(0o755)
                (job / "steps.jsonl").chmod(0o644)
            args = ["run"]
            if src:
                args += ["--in", self.runner.workspace_arg(self.workspace, src)]
            args += [
                "--script",
                self.runner.job_arg(job, "steps.jsonl"),
                "--out",
                self.runner.workspace_arg(self.workspace, dst),
                "--quiet",
            ]
            res = await self.runner.run(args, self.workspace, job_dir=job, writable=True)
            self._check(res, "run")
        finally:
            shutil.rmtree(job, ignore_errors=True)
        return await self._saved(dst, f"Saved {dst} ({len(steps)} step(s)).")

    async def _saved(self, rel: str, head: str) -> str:
        """Engine read-back of what was saved, so the reply quotes engine-computed values."""
        target = self.workspace / rel
        if not target.is_file():
            raise SpreadsheetError(f"GridCraft did not write {rel}")
        lines = [
            head,
            f"File: {rel} ({target.stat().st_size} bytes) — shown to the user as a downloadable file.",
        ]
        if target.suffix.lower() in (".xlsx", ".csv", ".tsv"):
            res = await self.runner.run(
                ["cat", self.runner.workspace_arg(self.workspace, rel), "--csv"], self.workspace
            )
            if res.code == 0:
                rows = res.stdout.splitlines()
                shown = rows[:PRINT_ROWS]
                lines.append("Values computed by the engine (first sheet, CSV):")
                lines.extend(shown)
                if len(rows) > PRINT_ROWS:
                    lines.append(f"[{len(rows) - PRINT_ROWS} more rows not shown]")
        lines.append("Quote numbers exactly as above. Reply in the language of the user's latest message.")
        return "\n".join(lines)
