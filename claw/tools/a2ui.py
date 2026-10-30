"""A2UI tools (POC, behind CLAW_A2UI_ENABLED): `present_ui` and
`openrouter_price_watch`. Both write a `.a2ui` artifact the chat renders as an
interactive card (web/src/a2ui/), so the card persists with the transcript.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from claw.a2ui import price_watch
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
    "DataTable, PriceChart). Read skill 'a2ui-cards' for the exact format. Allowed components: "
    + ", ".join(ALLOWED)
    + ". Pass `messages` = the A2UI v0.9 message list [createSurface, updateComponents, updateDataModel]. "
    'Button actions must be {"event": {"name": ..., "context": {key: {"path": "/..."}}}}; the click arrives as a normal user '
    "message starting with '[การ์ด]' (Thai card) or '[Card]' (other languages). Text is plain (no markdown/HTML). Write every card label, option and button in the "
    "language of the user's latest message (Thai in -> Thai card, English in -> English card), and your sentence around "
    "the card in that same language with no words from other languages. On VALIDATION_FAILED fix the listed paths and "
    "call again."
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
