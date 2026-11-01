# Spreadsheet tool (GridCraft) — spec

Status: POC on branch `feat/spreadsheet-gridcraft`, behind `CLAW_SPREADSHEET_ENABLED` (default **off**).

## Goal

Let the agent create, edit, convert and read spreadsheets for a user with a real spreadsheet
engine instead of hand-written openpyxl code: native formulas (`=SUM`, 500+ functions), charts,
tables, sorting/filtering and pivot tables, computed by the engine so the numbers the model quotes
come from the engine, not from the model. Engine: [GridCraft](https://github.com/storytold/gridcraft)
`gridcraft-cli` (Rust, MIT/Apache-2.0), wrapped as one PrivateClaw tool. Not its MCP server: that
server opens/saves any host path (`open_workbook` read `/etc/passwd` in testing) and keeps one
in-process workbook state, which does not fit per-user isolation.

## Tool: `spreadsheet`

One tool, `action` picks the operation. Every path is workspace-relative.

| action | inputs | does | CLI |
|---|---|---|---|
| `inspect` | `path` | sheets, used ranges, tables, charts, names | `info --json` |
| `read` | `path`, `range?`, `sheet?`, `formulas?` | displayed values as CSV (capped) | `cat --csv` |
| `evaluate` | `formula`, `path?`, `sheet?` | evaluate a formula against the file without changing it | `eval --json` |
| `convert` | `path`, `out`, `sheet?` | xlsx/xlsm/xls/xlsb/ods/csv/tsv → xlsx/csv/tsv/html | `convert` |
| `run` | `path?`, `steps`, `out` | open (or blank), run engine commands, save | `run --script` |
| `create` | `values` (2-D, first row = header), `steps?`, `out?`, `sheet_name?` | blank book → `range.setValues` at A1 → steps → save | `run --script` |

`run`/`create` return the saved path plus the engine's own read-back of the used range
(`--print used`, first rows only), so the reply quotes engine-computed totals.
Default `out`: `sheets/<slug>-<yyyymmdd-HHMMSS>.xlsx`. The saved file appears as a chat artifact
through the normal turn artifact scan (no change to `loop.py`).

## Security

- **Path confinement.** Paths are resolved against the user's workspace (`workspaces/<user_id>`);
  absolute paths (other than the `/workspace/` alias), `~`, `..` escapes, hidden path parts and
  symlinks resolving outside the workspace are rejected. Input suffixes:
  `.xlsx .xlsm .xls .xlsb .ods .csv .tsv`; output: `.xlsx .csv .tsv .html`.
- **Execution in the sandbox.** With `CLAW_SANDBOX__ENABLED=true` (claw2) each call is one
  `docker run --rm` of `claw-sandbox:latest` with the pinned **Linux** binary bind-mounted
  read-only, `--network none`, `--cap-drop ALL`, `no-new-privileges`, read-only root fs + tmpfs
  `/tmp`, the sandbox CPU/memory/pids limits, and only that user's workspace mounted
  (read-only for `inspect`/`read`/`evaluate`). The script goes in a per-call temp dir mounted
  read-only at `/job`. No shell: the binary is the entrypoint, arguments are an argv list.
  With the sandbox disabled it falls back to the native binary as a subprocess (cwd = workspace,
  minimal env) — weaker; documented, intended for dev only.
- **Command allow-list.** `steps[].command` must be in an explicit allow-list (cell/range values,
  fill, formatting, sheets, tables, charts, sparklines, conditional formats, sort/filter,
  remove duplicates, subtotal, pivot tables, freeze panes, names). Everything that touches files,
  windows, macros/scripts, links, pictures, external data, printing/PDF export or the app is
  refused (`file.*`, `macro.*`, `automate.*`, `data.fromTextCsv`, `data.getData`,
  `insert.picture`, `insert.link`, `link.open`, `file.exportPdf`, `window.*`, `app.*`, …).
  Params are scanned: keys like `path`/`file`/`url` and string values starting with `/`, `~`,
  `file:` or a URL scheme are refused (defence in depth; the container would not see host
  paths anyway).
- **Pinned version.** GridCraft `0.14.2`, installed by `scripts/install-gridcraft.sh` into
  `~/.softnix/claw/vendor/gridcraft/0.14.2/` with the release's published SHA-256 checked.
  The tool checks `gridcraft-cli version` once and refuses a mismatch. Upgrades = bump the pin
  + checksums + rerun tests.
- **Limits.** Input file ≤ 25 MB; `create` ≤ 50,000 cells; ≤ 300 steps; per-call timeout
  = min(sandbox timeout, 60 s); output to the model capped at 20,000 chars; at most 4
  concurrent GridCraft runs per process (semaphore) to protect the host.

## Thai PDF

GridCraft's PDF export does not embed fonts and maps Thai to a CJK font (garbage text in
testing). The tool refuses `.pdf` output and tells the model to use the existing `pdf` skill
(sandbox `exec` with Thai fonts) on the xlsx/csv it produced.

## Language

Tool description tells the model to reply in the language of the user's latest message, quote
numbers exactly as the tool returned them, and name sheets/headers in that language.

## Interplay with chart_from_data / A2UI and the xlsx skill

- `chart_from_data` = chart card **inside the chat** (A2UI). `spreadsheet` = a **file** the user
  downloads (native Excel chart, live formulas). Both can be used in one answer: build the file with
  `spreadsheet`, show the summary with `chart_from_data` on the same file.
- The `xlsx` built-in skill (openpyxl via `exec`) stays for things the engine can't do; the tool
  description says to prefer `spreadsheet` for creating/editing/reading workbooks.

## Install / update (CLAUDE.md rule 7)

- New: `scripts/install-gridcraft.sh` (idempotent, checksum-verified).
- New env: `CLAW_SPREADSHEET_ENABLED` (default false), `CLAW_SPREADSHEET_VENDOR_DIR` (optional).
  Added to `.env.example`.
- No new Python dependency, no migration, no sandbox-image change (binary is mounted, not baked).
- Follow-up: call the install script from `install.sh`/`claw update` when the flag is on.

## Out of scope

GridCraft MCP server, the desktop app/control channel, macros, PDF export, in-chat editing UI.

## Added during e2e on claw2 (2026-11-01)

- **No clobbering.** `convert`/`create`, and `run` writing to a file other than the one being edited,
  refuse an existing `out` unless `overwrite=true` (a model overwrote `data/monthly_sales_2026.xlsx`
  in testing). `run` with `path` and no `out` saves in place (csv → same name `.xlsx`).
  `run` without `path` onto an existing file is refused (it would start from a blank workbook).
- **Artifact-job scope.** `claw/core/runtime.py` narrows tools for "make a file" turns
  (`_ARTIFACT_CORE_TOOLS`); `spreadsheet` is added there (and to the quota/research write sets),
  otherwise the tool is invisible exactly when it is needed. No change to `loop.py`/EventBus.
- **Steering.** With the flag on, the `xlsx` built-in skill starts with a note to use `spreadsheet`
  first, including a pivot-table recipe for per-group totals.
- **Hints.** Unknown commands suggest close allowed ones; formula errors with Thai sheet names
  suggest quoting (`='ข้อมูล'!B2`); pivot errors say to open the data file with `path`;
  `evaluate` with cell references requires `path`.
