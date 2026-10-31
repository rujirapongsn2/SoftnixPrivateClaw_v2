"""OpenRouter Price Watch — every number is computed here, never by the model.

Prices on https://openrouter.ai/api/v1/models are USD per token as decimal
strings; we convert with Decimal to USD per 1M tokens. The API has no price
history, so deltas are taken against our own snapshot in the user's workspace
(`.a2ui-state/openrouter-prices.json`, a dot-dir the artifact scan skips).
A model the snapshot does not know is compared with the previous model of the
same provider (clearly labelled); with neither, it is marked "new".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from claw.a2ui import A2UI_VERSION, SOFTNIX_CATALOG_ID

MILLION = Decimal(1_000_000)
FLAT_EPSILON_PCT = Decimal("0.05")
_TH = timezone(timedelta(hours=7))


def per_million(raw: Any) -> Decimal | None:
    """USD/token string -> USD per 1M tokens (None for missing/negative/garbage)."""
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if d.is_nan() or d < 0:
        return None
    return (d * MILLION).quantize(Decimal("0.0001"))


def pct_change(current: Decimal, base: Decimal) -> Decimal | None:
    if base == 0:
        return None
    return ((current - base) / base * 100).quantize(Decimal("0.1"))


def direction(current: Decimal, base: Decimal | None) -> str:
    if base is None:
        return "new"
    if base == 0:
        return "flat" if current == 0 else "up"
    pct = (current - base) / base * 100
    if abs(pct) < FLAT_EPSILON_PCT:
        return "flat"
    return "up" if pct > 0 else "down"


@dataclass
class ModelPrice:
    id: str
    name: str
    provider: str
    created: int
    input: Decimal
    output: Decimal


@dataclass
class WatchRow:
    model: ModelPrice
    basis: str  # snapshot | prev_model | none
    baseline_label: str
    base_input: Decimal | None
    base_output: Decimal | None
    dir_input: str
    dir_output: str
    pct_input: Decimal | None
    pct_output: Decimal | None


def parse_models(payload: dict) -> list[ModelPrice]:
    out: list[ModelPrice] = []
    for m in payload.get("data") or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        pricing = m.get("pricing") or {}
        pin, pout = per_million(pricing.get("prompt")), per_million(pricing.get("completion"))
        created = m.get("created")
        if pin is None or pout is None or not isinstance(created, (int, float)):
            continue
        mid = m["id"]
        out.append(
            ModelPrice(
                id=mid,
                name=str(m.get("name") or mid)[:120],
                provider=mid.split("/", 1)[0],
                created=int(created),
                input=pin,
                output=pout,
            )
        )
    return out


# Card strings in the user's language. The model passes `lang`; anything that
# isn't Thai gets English (the model's own reply still follows the user).
_T = {
    "th": {
        "vs_snapshot": "เทียบกับข้อมูลที่บันทึกไว้ {when}",
        "vs_prev": "เทียบรุ่นก่อนของ {provider}: {prev}",
        "no_prev": "ไม่มีรุ่นก่อนให้เทียบ",
        "new": "ใหม่",
        "flat": "คงที่",
        "up_zero": "▲ (ฐานเดิม $0)",
        "title": "ราคาโมเดลใหม่บน OpenRouter ({days} วันล่าสุด)",
        "sub": "ดึงจาก openrouter.ai/api/v1/models เมื่อ {when} · โมเดลใหม่ {n} จาก {total} · หน่วย USD ต่อ 1 ล้านโทเคน · ▼ ถูกลง ▲ แพงขึ้น (ราคาขาออก)",
        "empty": "ไม่พบโมเดลที่เปิดตัวใน {days} วันล่าสุด",
        "chart": "ราคาขาเข้าเทียบขาออก (USD ต่อ 1 ล้านโทเคน)",
        "in": "ขาเข้า",
        "out": "ขาออก",
        "unit_out": "$/1M ขาออก",
        "col_model": "โมเดล",
        "col_date": "เปิดตัว",
        "col_basis": "เทียบกับ",
    },
    "en": {
        "vs_snapshot": "vs snapshot {when}",
        "vs_prev": "vs previous {provider} model: {prev}",
        "no_prev": "no previous model to compare",
        "new": "new",
        "flat": "flat",
        "up_zero": "▲ (from $0)",
        "title": "New models on OpenRouter (last {days} days)",
        "sub": "From openrouter.ai/api/v1/models at {when} · {n} new of {total} models · USD per 1M tokens · ▼ cheaper ▲ pricier (output price)",
        "empty": "No models released in the last {days} days",
        "chart": "Input vs output price (USD per 1M tokens)",
        "in": "Input",
        "out": "Output",
        "unit_out": "$/1M out",
        "col_model": "Model",
        "col_date": "Released",
        "col_basis": "Compared with",
    },
}


def norm_lang(lang: str | None) -> str:
    return "th" if (lang or "th").strip().lower().startswith("th") else "en"


def _t(lang: str, key: str, **kw: object) -> str:
    return _T[norm_lang(lang)][key].format(**kw)


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, _TH).strftime("%d/%m/%Y")


def compute(
    models: list[ModelPrice], snapshot: dict | None, now: int, days: int = 7, lang: str = "th"
) -> list[WatchRow]:
    snap_models = (snapshot or {}).get("models") or {}
    snap_when = (snapshot or {}).get("fetched_at")
    cutoff = now - days * 86_400
    recent = sorted((m for m in models if m.created >= cutoff), key=lambda m: (-m.created, m.id))
    rows: list[WatchRow] = []
    for m in recent:
        s = snap_models.get(m.id)
        if isinstance(s, dict) and per_million_raw_ok(s):
            bi, bo = Decimal(s["input"]), Decimal(s["output"])
            basis, label = "snapshot", _t(lang, "vs_snapshot", when=snap_when or "").strip()
        else:
            prev = _previous_model(models, m)
            if prev is not None:
                bi, bo = prev.input, prev.output
                basis, label = "prev_model", _t(lang, "vs_prev", provider=m.provider, prev=prev.id)
            else:
                bi = bo = None
                basis, label = "none", _t(lang, "no_prev")
        rows.append(
            WatchRow(
                model=m,
                basis=basis,
                baseline_label=label,
                base_input=bi,
                base_output=bo,
                dir_input=direction(m.input, bi),
                dir_output=direction(m.output, bo),
                pct_input=pct_change(m.input, bi) if bi is not None else None,
                pct_output=pct_change(m.output, bo) if bo is not None else None,
            )
        )
    return rows


def per_million_raw_ok(s: dict) -> bool:
    try:
        Decimal(str(s.get("input")))
        Decimal(str(s.get("output")))
        return True
    except (InvalidOperation, ValueError):
        return False


def _is_free_variant(m: ModelPrice) -> bool:
    return m.id.endswith(":free") or (m.input == 0 and m.output == 0)


def _variant(m: ModelPrice) -> str:
    """OpenRouter suffix variant (':free', ':batch', ...) — '' for the base model."""
    return m.id.split(":", 1)[1] if ":" in m.id else ""


def _previous_model(models: list[ModelPrice], m: ModelPrice) -> ModelPrice | None:
    """Latest model of the same provider created before `m`, like for like: the
    same ':variant' suffix and paid vs free, so a ':batch' or ':free' price is
    never the baseline of a regular one."""
    want_free, want_variant = _is_free_variant(m), _variant(m)
    cands = [
        o
        for o in models
        if o.provider == m.provider
        and o.created < m.created
        and o.id != m.id
        and _variant(o) == want_variant
        and _is_free_variant(o) == want_free
    ]
    return max(cands, key=lambda o: (o.created, o.id)) if cands else None


def snapshot_of(models: list[ModelPrice], now: int) -> dict:
    return {
        "fetched_at": datetime.fromtimestamp(now, _TH).strftime("%d/%m/%Y %H:%M ICT"),
        "fetched_ts": now,
        "models": {
            m.id: {"input": str(m.input), "output": str(m.output), "created": m.created} for m in models
        },
    }


def _f(d: Decimal | None) -> float:
    return float(d) if d is not None else 0.0


def _pct_text(p: Decimal | None, dir_: str, lang: str = "th") -> str:
    if dir_ == "new":
        return _t(lang, "new")
    if p is None:
        return {"flat": _t(lang, "flat"), "up": _t(lang, "up_zero")}.get(dir_, "")
    glyph = {"up": "▲", "down": "▼", "flat": "■"}[dir_]
    return f"{glyph} {'+' if p > 0 else ''}{p}%"


def build_messages(
    rows: list[WatchRow], fetched_label: str, total_models: int, days: int, surface_id: str, lang: str = "th"
) -> list[dict]:
    comps: list[dict] = [
        {"id": "root", "component": "Card", "child": "col"},
        {
            "id": "title",
            "component": "Text",
            "variant": "h3",
            "text": _t(lang, "title", days=days),
        },
        {
            "id": "sub",
            "component": "Text",
            "variant": "caption",
            "text": _t(lang, "sub", when=fetched_label, n=len(rows), total=total_models),
        },
    ]
    children = ["title", "sub"]
    if not rows:
        comps.append({"id": "empty", "component": "Text", "text": _t(lang, "empty", days=days)})
        children.append("empty")
    else:
        stat_ids = []
        for i, r in enumerate(rows[:6]):
            sid = f"stat{i}"
            stat = {
                "id": sid,
                "component": "DeltaStat",
                "label": r.model.name,
                "value": _f(r.model.output),
                "unit": _t(lang, "unit_out"),
                "direction": r.dir_output,
                "baseline": r.baseline_label
                if r.basis != "prev_model"
                else f"{r.baseline_label} (${_f(r.base_output):,.2f})",
            }
            if r.pct_output is not None:
                stat["deltaPct"] = float(r.pct_output)
            comps.append(stat)
            stat_ids.append(sid)
        comps.append({"id": "stats", "component": "Row", "children": stat_ids})
        comps.append(
            {
                "id": "chart",
                "component": "PriceChart",
                "title": _t(lang, "chart"),
                "unit": "$/1M",
                "seriesLabels": [_t(lang, "in"), _t(lang, "out")],
                "items": [
                    {"label": r.model.id, "a": _f(r.model.input), "b": _f(r.model.output)} for r in rows[:12]
                ],
            }
        )
        comps.append(
            {
                "id": "table",
                "component": "DataTable",
                "columns": [
                    {"key": "model", "label": _t(lang, "col_model")},
                    {"key": "date", "label": _t(lang, "col_date")},
                    {"key": "in", "label": f"{_t(lang, 'in')} $/1M", "align": "end"},
                    {"key": "out", "label": f"{_t(lang, 'out')} $/1M", "align": "end"},
                    {"key": "din", "label": f"Δ {_t(lang, 'in')}", "align": "end"},
                    {"key": "dout", "label": f"Δ {_t(lang, 'out')}", "align": "end"},
                    {"key": "basis", "label": _t(lang, "col_basis")},
                ],
                "rows": [
                    {
                        "model": r.model.id,
                        "date": _fmt_date(r.model.created),
                        "in": _plain(r.model.input),
                        "out": _plain(r.model.output),
                        "din": _pct_text(r.pct_input, r.dir_input, lang),
                        "dout": _pct_text(r.pct_output, r.dir_output, lang),
                        "basis": r.baseline_label,
                    }
                    for r in rows[:40]
                ],
            }
        )
        children += ["stats", "chart", "table"]
    comps.insert(1, {"id": "col", "component": "Column", "children": children})
    return [
        {
            "version": A2UI_VERSION,
            "createSurface": {"surfaceId": surface_id, "catalogId": SOFTNIX_CATALOG_ID},
        },
        {"version": A2UI_VERSION, "updateComponents": {"surfaceId": surface_id, "components": comps}},
    ]


def summary_for_model(rows: list[WatchRow], fetched_label: str, artifact: str, lang: str = "th") -> str:
    lines = [
        f"OK. Price card written to {artifact} and shown to the user (chart, arrows and table are already on screen).",
        f"Fetched {fetched_label}. Prices are USD per 1M tokens, computed by the tool. Do NOT recompute, round differently or invent numbers —",
        "quote these exactly if you mention any, and keep your reply to a short summary (2-4 sentences) written entirely in the",
        "language of the user's latest message (no words from other languages).",
        "id | input | output | Δinput | Δoutput | basis",
    ]
    for r in rows[:40]:
        lines.append(
            f"{r.model.id} | {_plain(r.model.input)} | {_plain(r.model.output)} | "
            f"{_pct_text(r.pct_input, r.dir_input, lang)} | {_pct_text(r.pct_output, r.dir_output, lang)} | {r.baseline_label}"
        )
    if not rows:
        lines.append("(no models created in the window)")
    return "\n".join(lines)


def _plain(d: Decimal) -> str:
    """Decimal without exponent or trailing zeros: 10.0000 -> '10', 0.1500 -> '0.15'."""
    return format(d.normalize(), "f")
