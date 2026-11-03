"""A2UI tools (POC, behind CLAW_A2UI_ENABLED): `present_ui`, `chart_from_data`,
`openrouter_price_watch` and `weather_now`. Each writes a `.a2ui` artifact the chat renders as an
interactive card (web/src/a2ui/), so the card persists with the transcript.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
import os
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from claw.a2ui import chart_data, price_watch, weather
from claw.a2ui.validate import ALLOWED, normalize
from claw.a2ui.writer import write_artifact
from claw.config import WeatherSettings
from claw.security.ssrf import ResponseTooLarge, read_bounded
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


# Components whose values must be computed by their own tool, never typed into present_ui.
_TOOL_ONLY = {
    "Chart": "charts are made by tool chart_from_data (it computes the numbers from the file, or from `rows` you "
    "pass with source='inline'); remove the Chart here and call chart_from_data",
    "WeatherCard": "weather cards are made by tool weather_now (it fetches the real conditions); remove the "
    "WeatherCard here and call weather_now",
    "LocationButton": "the location button is shown only by tool weather_now (call it with no location); remove it here",
}


def _tool_only_errors(messages: list[dict]) -> list[str]:
    found = {
        c.get("component")
        for m in messages
        for c in (m.get("updateComponents") or {}).get("components", [])
        if isinstance(c, dict)
    }
    return [f"{name}: {why}" for name, why in _TOOL_ONLY.items() if name in found]


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
        if canonical is not None and not errors:
            errors = _tool_only_errors(canonical)
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


_reverse_gate = asyncio.Lock()
_reverse_last = [0.0]
_REVERSE_SPACING_S = 1.0


class WeatherNowTool(Tool):
    name = "weather_now"
    description = (
        "Show the current weather (condition, temperature, local date and time) for a place as a weather card in the "
        "chat, from the public Open-Meteo API (no key). Use it for any 'weather now / today / สภาพอากาศ / อากาศ' "
        "question; never state weather from memory. Pass `location` = the place name, best in English (e.g. "
        "'Bangkok', 'Chiang Mai'), or 'lat,lon'. If the user named no place and you do not know where they are, call "
        "it with NO location: it shows a card where they share their current location or type a city, and their "
        f"answer arrives as a card message ('{weather.LOCATION_EVENT}' with `location`); then call weather_now again with "
        "that `location` exactly. Set `hourly`=true when the user asks whether or when it will rain (ฝนจะตกไหม, พยากรณ์ฝน) "
        "or about the next few hours: the card then adds the hourly chance of rain for the next 12 hours. It has no "
        "forecast beyond that. Set `lang` to the language of the user's latest message ('th' or 'en'; 'en' for any "
        "other language) and reply in that language."
    )
    writes_workspace = True
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "maxLength": 100,
                "description": "Place name (e.g. 'Bangkok') or 'lat,lon'. Omit to ask the user where they are.",
            },
            "lang": {"type": "string", "enum": ["th", "en"], "description": "Language of the user's latest message."},
            "unit": {"type": "string", "enum": list(weather.UNITS), "description": "Default celsius."},
            "hourly": {
                "type": "boolean",
                "description": "Add the chance of rain for the next 12 hours (questions about rain or later today).",
            },
        },
    }

    ASK_WINDOW_S = 600

    def __init__(self, workspace: Path, settings: WeatherSettings | None = None, fetch: Any = None):
        self.workspace = workspace
        self.settings = settings or WeatherSettings()
        self._fake_fetch = fetch
        self._asked: dict[str, float] = {}  # session -> when the location card was last shown

    @asynccontextmanager
    async def _session(self):
        """One HTTP client for the geocode + forecast pair of a call, so both reuse its connection pool."""
        if self._fake_fetch is not None:
            yield self._fake_fetch
            return
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, headers={"User-Agent": "SoftnixPrivateClaw/a2ui"}
        ) as client:

            async def fetch(url: str, params: dict) -> Any:
                if self.settings.api_key and url in (self.settings.geocode_url, self.settings.forecast_url):
                    params = {**params, "apikey": self.settings.api_key}
                async with client.stream("GET", url, params=params) as resp:
                    resp.raise_for_status()
                    try:
                        body = await read_bounded(resp, weather.MAX_RESPONSE_BYTES)
                    except ResponseTooLarge as exc:
                        raise weather.WeatherError("response too large") from exc
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise weather.WeatherError("response is not JSON") from exc

            yield fetch

    def _recently_asked(self, key: str) -> bool:
        now = time.monotonic()
        for k in [k for k, t in self._asked.items() if now - t > self.ASK_WINDOW_S]:
            del self._asked[k]
        return key in self._asked

    async def _place(self, fetch: Any, query: str, lang: str) -> weather.Place | None:
        key = (query.casefold(), lang)
        hit = weather.places.get(key)
        if hit is not None:
            return hit
        if key in weather.unknown_places:
            return None
        payload = await fetch(
            self.settings.geocode_url, {"name": query, "count": 1, "language": lang, "format": "json"}
        )
        place = weather.parse_place(payload)
        if place is None:
            weather.unknown_places.put(key, True)
        else:
            weather.places.put(key, place)
        return place

    async def _name(self, fetch: Any, here: weather.Place, lang: str) -> weather.Place:
        """Name shared coordinates; on any failure keep the coordinates label, the weather still shows."""
        if not self.settings.reverse_geocode_url:
            return here
        key = (here.latitude, here.longitude, lang)
        hit = weather.place_names.get(key)
        if hit is not None:
            return hit
        try:
            async with _reverse_gate:  # the public Nominatim allows one request per second
                wait = _REVERSE_SPACING_S - (time.monotonic() - _reverse_last[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                _reverse_last[0] = time.monotonic()
                payload = await fetch(
                    self.settings.reverse_geocode_url,
                    {"lat": here.latitude, "lon": here.longitude, "format": "jsonv2", "zoom": 10, "accept-language": lang},
                )
        except Exception as exc:
            logger.warning("weather_now reverse geocoding failed: {}", type(exc).__name__)
            return here
        named = weather.parse_reverse(payload, here)
        if named is None:
            return here
        weather.place_names.put(key, named)
        return named

    async def _current(self, fetch: Any, place: weather.Place, unit: str) -> weather.Current:
        key = (round(place.latitude, 2), round(place.longitude, 2), unit)
        hit = weather.conditions.get(key)
        if hit is None:
            hit = (await self._fetch_current(fetch, place, unit), time.monotonic())
            weather.conditions.put(key, hit)
        cur, fetched_at = hit
        now = weather.local_now(cur, time.monotonic() - fetched_at)
        return replace(cur, hours=weather.upcoming(cur.hours, now))

    async def _fetch_current(self, fetch: Any, place: weather.Place, unit: str) -> weather.Current:
        payload = await fetch(
            self.settings.forecast_url,
            {
                "latitude": place.latitude,
                "longitude": place.longitude,
                "current": "temperature_2m,weather_code,is_day",
                "hourly": "precipitation_probability,precipitation",
                "forecast_days": 2,
                "temperature_unit": unit,
                "timezone": "auto",
            },
        )
        return weather.parse_current(payload)

    async def execute(
        self, location: str = "", lang: str = "th", unit: str = "celsius", hourly: bool = False, **_: Any
    ) -> str:
        query = " ".join(str(location or "").split())
        if len(query) > 100:
            return "Error: `location` must be a place name of at most 100 characters."
        lang = weather.norm_lang(lang)
        unit = unit if unit in weather.UNITS else "celsius"
        session = current_session_id.get() or "-"
        if not query:
            if self._recently_asked(session):
                return (
                    "Error: the location card was already shown in this chat and came back without a place. Do not "
                    "show it again: ask the user in plain text, in their language, which city they mean."
                )
            self._asked[session] = time.monotonic()
            rel = await asyncio.to_thread(write_artifact, self.workspace, weather.build_location_request(lang), "weather-location")
            return (
                f"OK. A card asking where the user is was shown ({rel}). End your turn now with at most one short sentence "
                "in the user's language; do not guess a place. Their answer arrives as the next user message "
                f"('{weather.LOCATION_EVENT}' with `location`): call weather_now with that `location` exactly."
            )
        try:
            async with self._session() as fetch:
                here = weather.parse_coordinates(query, lang)
                place = await self._name(fetch, here, lang) if here else await self._place(fetch, query, lang)
                if place is None:
                    return (
                        f"Error: no place named {query!r} was found. Ask the user for a clearer place name (a city "
                        "name in English works best). Do not guess the weather."
                    )
                cur = await self._current(fetch, place, unit)
        except weather.WeatherError as exc:
            logger.warning("weather_now got an unusable response: {}", exc)
            return f"Error: {weather.SOURCE} returned unusable data ({exc}). Tell the user; do not guess the weather."
        except Exception as exc:  # network/HTTP/JSON problems are the model's to report
            # Not the exception itself: an httpx error's text carries the request URL, and so the API key.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("weather_now fetch failed: {} (HTTP {})", type(exc).__name__, status)
            return f"Error: could not reach {weather.SOURCE} ({type(exc).__name__}). Tell the user; do not guess the weather."
        self._asked.pop(session, None)
        # Some providers send booleans as strings.
        hourly = hourly is True or (isinstance(hourly, str) and hourly.strip().lower() in ("true", "1", "yes"))
        messages = weather.build_messages(place, cur, lang, unit, hourly)
        rel = await asyncio.to_thread(write_artifact, self.workspace, messages, "weather")
        return weather.summary_for_model(place, cur, lang, unit, rel, hourly)


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
