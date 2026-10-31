"""A2UI tools (POC, behind CLAW_A2UI_ENABLED): `present_ui` and
`openrouter_price_watch`. Both write a `.a2ui` artifact the chat renders as an
interactive card (web/src/a2ui/), so the card persists with the transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from claw.a2ui import chart_data, price_watch
from claw.a2ui.validate import ALLOWED, normalize
from claw.a2ui.writer import write_artifact
from claw.core.turn_context import current_session_id
from claw.tools.base import Tool

MAX_RETRIES = 2  # validation retries before telling the model to fall back to text
_RETRY_WINDOW_S = 600

_PRESENT_UI_DESCRIPTION = (
    "Show the user an interactive A2UI v0.9 card (rendered in the chat with the Softnix catalog) instead of plain text. "
    "Use it to ask a clarifying question with clickable choices when a request is too broad (e.g. 'สร้างรายงาน' with no "
    "format/audience/topic) — then STOP and wait for the user's click; or to present structured results (DeltaStat, "
    "DataTable). For charts of data use chart_from_data instead (it computes the numbers). Read skill 'a2ui-cards' for the exact format. Allowed components: "
    + ", ".join(ALLOWED)
    + ". Pass `messages` = the A2UI v0.9 message list [createSurface, updateComponents, updateDataModel]. "
    'Button actions must be {"event": {"name": ..., "context": {key: {"path": "/..."}}}}; the click arrives as a normal user '
    "message starting with '[การ์ด]' (Thai card) or '[Card]' (other languages). Text is plain (no markdown/HTML). Write every card label, option and button in the "
    "language of the user's latest message (Thai in -> Thai card, English in -> English card), and your sentence around "
    "the card in that same language with no words from other languages. On VALIDATION_FAILED fix the listed paths and "
    "call again."
)


def _has_chart(messages: list[dict]) -> bool:
    return any(
        c.get("component") == "Chart"
        for m in messages
        for c in (m.get("updateComponents") or {}).get("components", [])
        if isinstance(c, dict)
    )


class PresentUiTool(Tool):
    name = "present_ui"
    description = _PRESENT_UI_DESCRIPTION
    writes_workspace = True
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short slug-like name for the card file, e.g. 'report-clarify'.",
            },
            "messages": {
                "description": "A2UI v0.9 message list (array of message objects) — or the same as a JSON string.",
                "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "string"}],
            },
        },
        "required": ["messages"],
    }

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._failures: dict[str, tuple[int, float]] = {}

    def _fail(self, key: str, errors: list[str]) -> str:
        count, ts = self._failures.get(key, (0, 0.0))
        if time.monotonic() - ts > _RETRY_WINDOW_S:
            count = 0
        count += 1
        if count > MAX_RETRIES:
            self._failures.pop(key, None)
            return (
                "Error: VALIDATION_FAILED and the retry limit was reached. Do NOT call present_ui again for this answer — "
                "reply to the user in plain text/markdown in the user's language instead (e.g. a numbered list of options)."
            )
        self._failures[key] = (count, time.monotonic())
        listed = "\n".join(f"- {e}" for e in errors[:15])
        return f"Error: VALIDATION_FAILED (attempt {count}/{MAX_RETRIES + 1}). Fix these and call present_ui again:\n{listed}"

    async def execute(self, messages: Any = None, title: str = "card", **_: Any) -> str:
        key = current_session_id.get() or "-"
        canonical, errors = normalize(messages, fallback_surface_id=(title or "card")[:40])
        if canonical is not None and not errors and _has_chart(canonical):
            errors = ["Chart: charts are made by tool chart_from_data (it computes the numbers from the file, or from "
                      "`rows` you pass with source='inline'); remove the Chart here and call chart_from_data"]
        if errors or canonical is None:
            return self._fail(key, errors or ["payload: invalid"])
        self._failures.pop(key, None)
        rel = await asyncio.to_thread(write_artifact, self.workspace, canonical, title or "card")
        return (
            f"OK. Card shown to the user ({rel}). If it asks the user something, end your turn now with at most one short "
            "sentence — do not answer on the user's behalf; their click arrives as the next user message. Write that sentence "
            "in the user's language only (Thai: call it การ์ด — never the English word 'card' or any Chinese word)."
        )


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
SNAPSHOT_REL = ".a2ui-state/openrouter-prices.json"


class OpenRouterPriceWatchTool(Tool):
    name = "openrouter_price_watch"
    description = (
        "Compare prices of models newly released on OpenRouter (public API, no key). Fetches "
        "openrouter.ai/api/v1/models, keeps models created in the last `days` days, computes USD per 1M tokens and "
        "▲/▼ deltas IN CODE (vs the last stored snapshot, or vs the provider's previous model on first sight) and shows "
        "the user a chart + delta cards + table. Use this for any 'compare OpenRouter model prices/new models this week' "
        "request; never compute those numbers yourself. Set `lang` to the language of the user's latest message ('th' or "
        "'en'; use 'en' for any other language) so the card's labels match, and write your reply in that language."
    )
    writes_workspace = True
    idempotent = True
    parameters = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 31, "description": "Window, default 7."},
            "lang": {
                "type": "string",
                "enum": ["th", "en"],
                "description": "Language of the user's latest message; card labels use it. Default 'th'.",
            },
        },
    }

    def __init__(self, workspace: Path, fetch: Any = None):
        self.workspace = workspace
        self._fetch = fetch or self._default_fetch

    @staticmethod
    async def _default_fetch() -> dict:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.get(
                OPENROUTER_MODELS_URL, headers={"User-Agent": "SoftnixPrivateClaw/a2ui-poc"}
            )
            resp.raise_for_status()
            return resp.json()

    def _load_snapshot(self) -> dict | None:
        path = self.workspace / SNAPSHOT_REL
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_snapshot(self, snap: dict) -> None:
        path = self.workspace / SNAPSHOT_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    async def execute(self, days: int = 7, lang: str = "th", **_: Any) -> str:
        days = max(1, min(int(days or 7), 31))
        try:
            payload = await self._fetch()
        except Exception as exc:  # network/HTTP/JSON problems are the model's to report
            logger.warning("openrouter_price_watch fetch failed: {!r}", exc)
            return f"Error: could not fetch {OPENROUTER_MODELS_URL} ({type(exc).__name__}). Tell the user and do not guess prices."
        models = price_watch.parse_models(payload if isinstance(payload, dict) else {})
        if not models:
            return "Error: OpenRouter returned no parsable models. Tell the user; do not guess prices."
        now = int(time.time())
        snapshot = await asyncio.to_thread(self._load_snapshot)
        lang = price_watch.norm_lang(lang)
        rows = price_watch.compute(models, snapshot, now, days, lang)
        new_snap = price_watch.snapshot_of(models, now)
        messages = price_watch.build_messages(
            rows, new_snap["fetched_at"], len(models), days, "openrouter-price-watch", lang
        )
        rel = await asyncio.to_thread(write_artifact, self.workspace, messages, "openrouter-prices")
        await asyncio.to_thread(self._save_snapshot, new_snap)
        return price_watch.summary_for_model(rows, new_snap["fetched_at"], rel, lang)


_CHART_DESCRIPTION = (
    "Use this for EVERY request to chart, graph, plot or visualize data (bar chart, line graph, pie, กราฟ, แผนภูมิ) — it "
    "shows an interactive chart right in the chat. Do not build charts with exec/matplotlib/xlsx/HTML/render_diagram "
    "unless the user explicitly asks for a file (PNG/Excel/PowerPoint) — 'make/create/draw a bar chart from X.csv' means "
    "this tool. It is always available when listed. Always pass `source` = the data file's workspace path. "
    "The tool reads a CSV/TSV/XLSX/JSON file (or inline `rows`), groups and "
    "aggregates it IN CODE (Decimal; sum/avg/count/min/max, date bucketing, ▲▼ % change vs previous period) and shows "
    "the user a chart card (bar, line, area, pie, donut, stackedBar, hbar, progress) with a data-table toggle. Use it "
    "whenever the user wants a chart/graph/visual of data, or totals/trends/shares where a chart helps — never type "
    "chart numbers into present_ui yourself. Pick: line/area = trend over time (set x_grain); bar = compare categories "
    "or periods; stackedBar = parts per period (series_by); hbar = ranking/long labels; pie/donut = share of a whole "
    "(<=12 slices, one y); progress = values vs a target. Set good_direction='down' for costs, errors, failures, "
    "latency. To count events/rows per x set agg='count' and omit y; otherwise y (the number column) is required. Write title/x_label/y_label/series_labels and `lang` in the language of the user's latest message. "
    "After it returns, reply briefly in that language, quoting numbers exactly as returned."
)


class ChartFromDataTool(Tool):
    name = "chart_from_data"
    description = _CHART_DESCRIPTION
    writes_workspace = True
    parameters = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "REQUIRED: workspace path of the .csv/.tsv/.xlsx/.json data file, e.g. 'data/sales.csv'. "
                "Use 'inline' only when you pass `rows` instead.",
            },
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Inline records (e.g. parsed from another tool's output) instead of a file; max 5000.",
            },
            "sheet": {"type": "string", "description": "XLSX sheet name (default: first sheet)."},
            "chart_type": {"type": "string", "enum": list(chart_data.CHART_TYPES)},
            "x": {"type": "string", "description": "Column for categories / time axis (group-by key)."},
            "y": {
                "description": "REQUIRED: the number column(s) to aggregate, e.g. 'amount' (NOT the axis title - that is "
                "y_label). Several columns -> several series. With agg='count', any column (counts non-empty cells).",
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}, "maxItems": 8}],
            },
            "agg": {
                "type": "string",
                "enum": list(chart_data.AGGS),
                "description": "Default 'sum' (needs y). Use 'count' with no y to count rows/events per x.",
            },
            "series_by": {"type": "string", "description": "Column whose values become series (one y). For stackedBar/multi-line."},
            "x_grain": {"type": "string", "enum": list(chart_data.GRAINS), "description": "Bucket a date column x by period."},
            "where": {"type": "object", "description": "Equality filters {column: value}."},
            "sort": {"type": "string", "enum": list(chart_data.SORTS), "description": "Default auto (time -> x; pie/hbar -> value_desc)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "Keep top N categories after sorting."},
            "delta": {"type": "string", "enum": ["none", "previous"], "description": "'previous' adds ▲▼ % vs the previous category/period."},
            "good_direction": {"type": "string", "enum": ["up", "down"], "description": "'down' when lower is better (costs, failures)."},
            "title": {"type": "string", "maxLength": 200},
            "x_label": {"type": "string", "maxLength": 60},
            "y_label": {"type": "string", "maxLength": 60},
            "unit": {"type": "string", "maxLength": 20, "description": "e.g. '฿', 'USD', '%', 'ครั้ง'."},
            "decimals": {"type": "integer", "minimum": 0, "maximum": 6},
            "series_labels": {"type": "object", "description": "Rename series {column_or_value: display label}."},
            "lang": {"type": "string", "enum": ["th", "en"], "description": "Language of the user's latest message."},
            "show_table": {"type": "boolean", "description": "Open the data table by default."},
        },
        "required": ["source", "chart_type", "x", "y"],
    }

    MAX_INLINE_ROWS = 5000

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._failures: dict[str, tuple[int, float]] = {}

    def _fail(self, key: str, error: str) -> str:
        count, ts = self._failures.get(key, (0, 0.0))
        if time.monotonic() - ts > _RETRY_WINDOW_S:
            count = 0
        count += 1
        if count > MAX_RETRIES:
            self._failures.pop(key, None)
            return (
                f"Error: {error}\nThe retry limit was reached. Do NOT call chart_from_data again for this answer — tell the "
                "user in plain text (in the user's language) what is missing, or show the figures you already have from "
                "tool output as a markdown table. Do not invent numbers."
            )
        self._failures[key] = (count, time.monotonic())
        return f"Error: CHART_FAILED (attempt {count}/{MAX_RETRIES + 1}): {error}\nFix the arguments and call chart_from_data again."

    def _resolve(self, source: str) -> Path:
        s = source.strip()
        if s.startswith("/workspace/"):
            s = s[len("/workspace/") :]
        root = self.workspace.resolve()
        p = (root / s).resolve()
        if not p.is_relative_to(root):
            raise chart_data.ChartDataError("source must be a file inside the workspace")
        if not p.is_file():
            raise chart_data.ChartDataError(f"file not found: {source}")
        return p

    def _data_files_hint(self) -> str:
        """Up to 15 data files (skipping hidden dirs and ui/), scanning at most 2000 entries."""
        root = self.workspace.resolve()
        found: list[str] = []
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not (dirpath == str(root) and d == "ui"))
            for name in sorted(filenames):
                seen += 1
                if Path(name).suffix.lower() in chart_data.SUFFIXES and not name.startswith("."):
                    found.append((Path(dirpath) / name).relative_to(root).as_posix())
                if len(found) >= 15 or seen > 2000:
                    break
            if len(found) >= 15 or seen > 2000:
                break
        return f". Data files in the workspace: {found}" if found else ""

    async def execute(self, **kw: Any) -> str:
        key = current_session_id.get() or "-"
        try:
            y = kw.get("y")
            ys = [y] if isinstance(y, str) and y else [str(v) for v in (y or [])]
            lang = "en" if kw.get("lang") == "en" else "th"
            limit = kw.get("limit")
            decimals = kw.get("decimals")
            spec = chart_data.ChartSpec(
                chart_type=str(kw.get("chart_type") or ""),
                x=str(kw.get("x") or ""),
                y=ys,
                agg=str(kw.get("agg") or "sum"),
                series_by=kw.get("series_by") or None,
                x_grain=kw.get("x_grain") or None,
                where=kw.get("where") if isinstance(kw.get("where"), dict) else {},
                sort=str(kw.get("sort") or "auto"),
                limit=int(limit) if limit not in (None, "") else None,
                delta=str(kw.get("delta") or "none"),
                good_direction="down" if kw.get("good_direction") == "down" else "up",
                title=str(kw.get("title") or ""),
                x_label=str(kw.get("x_label") or ""),
                y_label=str(kw.get("y_label") or ""),
                unit=str(kw.get("unit") or ""),
                decimals=int(decimals) if decimals not in (None, "") else None,
                series_labels={str(a): str(b) for a, b in (kw.get("series_labels") or {}).items()}
                if isinstance(kw.get("series_labels"), dict)
                else {},
                lang=lang,
                show_table=bool(kw.get("show_table")),
            )
            if spec.decimals is not None and not 0 <= spec.decimals <= 6:
                raise chart_data.ChartDataError("decimals: 0-6")
            if not spec.x:
                raise chart_data.ChartDataError("x: required")
            rows = kw.get("rows")
            source = kw.get("source")
            if source in ("inline", "rows"):
                source = None
            if rows is not None:
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise chart_data.ChartDataError("rows: list of objects")
                if len(rows) > self.MAX_INLINE_ROWS:
                    raise chart_data.ChartDataError(f"rows: max {self.MAX_INLINE_ROWS}; save to a file and pass `source`")
                label = "ข้อมูลจากเครื่องมือ" if lang == "th" else "tool output"
            elif source:
                path = self._resolve(str(source))
                rows = await asyncio.to_thread(chart_data.load_rows, path, kw.get("sheet") or None)
                label = path.relative_to(self.workspace.resolve()).as_posix()
            else:
                raise chart_data.ChartDataError(
                    "`source` is missing: pass the data file's workspace path, e.g. source='data/sales.csv'"
                    + self._data_files_hint()
                )
            result = chart_data.aggregate(rows, spec)
            messages = chart_data.build_messages(spec, result, label)
        except chart_data.ChartDataError as exc:
            return self._fail(key, str(exc))
        except (ValueError, TypeError, OSError) as exc:
            logger.warning("chart_from_data failed: {!r}", exc)
            return self._fail(key, f"could not read the data ({type(exc).__name__}: {str(exc)[:200]})")
        canonical, errors = normalize(messages, fallback_surface_id="chart")
        if errors or canonical is None:  # should not happen: our own builder
            logger.warning("chart_from_data built an invalid card: {}", errors[:5])
            return self._fail(key, "internal card validation failed: " + "; ".join(errors[:5]))
        self._failures.pop(key, None)
        rel = await asyncio.to_thread(write_artifact, self.workspace, canonical, f"chart-{spec.chart_type}")
        return chart_data.summary_for_model(spec, result, rel)
