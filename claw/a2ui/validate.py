"""Server-side gate for agent-authored A2UI payloads (mirror of web/src/a2ui/protocol.ts).

Normalizes the v0.9 message list (or the compact ``{components, data}`` form)
to the canonical list pinned to the Softnix catalog and checks it against the
catalog's structural rules. Errors are returned (never raised) as
``path: message`` strings so the model can fix its payload — the A2UI spec's
prompt → generate → validate loop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from claw.a2ui import A2UI_VERSION, SOFTNIX_CATALOG_ID

MAX_COMPONENTS = 120
MAX_STRING = 2_000
MAX_DEPTH = 12
MAX_PAYLOAD_CHARS = 60_000

# component -> required properties (keep in step with web/src/a2ui/catalog.tsx)
REQUIRED: dict[str, tuple[str, ...]] = {
    "Text": ("text",),
    "Column": ("children",),
    "Row": ("children",),
    "Card": ("child",),
    "Divider": (),
    "Button": ("child", "action"),
    "ChoicePicker": ("options", "value"),
    "TextField": ("label",),
    "DeltaStat": ("label", "value"),
    "PriceChart": ("items",),
    "DataTable": ("columns", "rows"),
    "Chart": ("chartType", "categories", "series"),
}

# keep in step with web/src/a2ui/chartMath.ts
CHART_TYPES = ("bar", "line", "area", "pie", "donut", "stackedBar", "hbar", "progress")
CHART_MAX_CATEGORIES = 60
CHART_MAX_SERIES = 8
CHART_MAX_POINTS = 480
CHART_MAX_PIE = 12
CHART_MAX_LABEL = 60
ALLOWED = tuple(REQUIRED)
_FORBIDDEN_KEYS = {"__proto__", "constructor", "prototype"}


def _walk(value: Any, depth: int, path: str, errors: list[str]) -> None:
    if depth > MAX_DEPTH:
        errors.append(f"{path}: nested too deep (max {MAX_DEPTH})")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            errors.append(f"{path}: string longer than {MAX_STRING} chars")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk(v, depth + 1, f"{path}[{i}]", errors)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in _FORBIDDEN_KEYS:
                errors.append(f"{path}.{k}: forbidden key")
            _walk(v, depth + 1, f"{path}.{k}", errors)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_component(i: int, c: dict, errors: list[str]) -> None:
    where = f"components[{i}]({c.get('id')!s})"
    name = c.get("component")
    for prop in REQUIRED[name]:
        if prop not in c:
            errors.append(f"{where}.{prop}: required for {name}")
    if name == "Button":
        action = c.get("action")
        if isinstance(action, dict) and "functionCall" in action:
            errors.append(
                f'{where}.action.functionCall: not allowed, use {{"event": {{"name": ..., "context": {{...}}}}}}'
            )
        elif not (
            isinstance(action, dict) and isinstance(action.get("event"), dict) and action["event"].get("name")
        ):
            errors.append(f'{where}.action: must be {{"event": {{"name": "...", "context": {{...}}}}}}')
    elif name == "ChoicePicker":
        opts = c.get("options")
        if (
            not isinstance(opts, list)
            or not opts
            or not all(isinstance(o, dict) and "label" in o and isinstance(o.get("value"), str) for o in opts)
        ):
            errors.append(f"{where}.options: non-empty list of {{label, value: string}}")
        val = c.get("value")
        if not (isinstance(val, list) or (isinstance(val, dict) and isinstance(val.get("path"), str))):
            errors.append(f'{where}.value: a string list or {{"path": "/..."}} binding')
    elif name == "DeltaStat":
        if "value" in c and not _is_num(c["value"]):
            errors.append(f"{where}.value: must be a number")
        if c.get("direction") not in (None, "up", "down", "flat", "new"):
            errors.append(f"{where}.direction: one of up|down|flat|new")
    elif name == "PriceChart":
        items = c.get("items")
        if (
            not isinstance(items, list)
            or not (1 <= len(items) <= 40)
            or not all(
                isinstance(it, dict)
                and isinstance(it.get("label"), str)
                and _is_num(it.get("a"))
                and _is_num(it.get("b"))
                for it in items
            )
        ):
            errors.append(f"{where}.items: 1-40 of {{label: string, a: number, b: number}}")
    elif name == "DataTable":
        cols = c.get("columns")
        if (
            not isinstance(cols, list)
            or not cols
            or not all(isinstance(x, dict) and "key" in x and "label" in x for x in cols)
        ):
            errors.append(f"{where}.columns: non-empty list of {{key, label}}")
        if not isinstance(c.get("rows"), list):
            errors.append(f"{where}.rows: must be a list of objects")
    elif name == "Chart":
        errors.extend(f"{where}.{e}" for e in validate_chart(c))
    elif name in ("Column", "Row") and not isinstance(c.get("children"), list):
        errors.append(f"{where}.children: must be a list of component ids")


def _finite(v: Any) -> bool:
    return _is_num(v) and v == v and v not in (float("inf"), float("-inf"))


def validate_chart(c: dict) -> list[str]:
    """Structural rules of the Softnix `Chart` component; errors are 'prop: message'."""
    errs: list[str] = []
    ctype = c.get("chartType")
    if ctype not in CHART_TYPES:
        errs.append(f"chartType: one of {'|'.join(CHART_TYPES)}")
    cats = c.get("categories")
    if not (
        isinstance(cats, list)
        and 1 <= len(cats) <= CHART_MAX_CATEGORIES
        and all(isinstance(x, str) and len(x) <= CHART_MAX_LABEL for x in cats)
    ):
        errs.append(f"categories: 1-{CHART_MAX_CATEGORIES} strings (max {CHART_MAX_LABEL} chars each)")
        return errs
    series = c.get("series")
    if not (isinstance(series, list) and 1 <= len(series) <= CHART_MAX_SERIES):
        errs.append(f"series: 1-{CHART_MAX_SERIES} items of {{label, values}}")
        return errs
    if len(cats) * len(series) > CHART_MAX_POINTS:
        errs.append(f"series: too many points (max {CHART_MAX_POINTS})")
    for i, s in enumerate(series):
        if not isinstance(s, dict) or not isinstance(s.get("label"), str) or len(s["label"]) > CHART_MAX_LABEL:
            errs.append(f"series[{i}].label: string (max {CHART_MAX_LABEL} chars)")
            continue
        vals = s.get("values")
        if not isinstance(vals, list) or len(vals) != len(cats):
            errs.append(f"series[{i}].values: one number (or null) per category")
        elif not all(v is None or _finite(v) for v in vals):
            errs.append(f"series[{i}].values: numbers or null only")
        elif ctype in ("pie", "donut", "progress") and any(v is not None and v < 0 for v in vals):
            errs.append(f"series[{i}].values: {ctype} values must be >= 0")
        deltas = s.get("deltas")
        if deltas is not None and (
            not isinstance(deltas, list)
            or len(deltas) != len(cats)
            or not all(
                d is None
                or (
                    isinstance(d, dict)
                    and d.get("direction") in ("up", "down", "flat", "new")
                    and (d.get("pct") is None or _finite(d.get("pct")))
                )
                for d in deltas
            )
        ):
            errs.append(f"series[{i}].deltas: one {{pct, direction}} (or null) per category")
        extra = set(s) - {"label", "values", "deltas"}
        if extra:
            errs.append(f"series[{i}]: unknown keys {sorted(extra)}")
    if ctype in ("pie", "donut"):
        if len(series) != 1:
            errs.append(f"series: {ctype} takes exactly one series")
        if len(cats) > CHART_MAX_PIE:
            errs.append(f"categories: {ctype} takes at most {CHART_MAX_PIE} slices")
    if c.get("goodDirection") not in (None, "up", "down"):
        errs.append("goodDirection: up|down")
    if c.get("decimals") is not None and not (isinstance(c["decimals"], int) and 0 <= c["decimals"] <= 6):
        errs.append("decimals: integer 0-6")
    for key, cap in (("title", 200), ("unit", 20), ("xLabel", CHART_MAX_LABEL), ("yLabel", CHART_MAX_LABEL)):
        if c.get(key) is not None and not (isinstance(c[key], str) and len(c[key]) <= cap):
            errs.append(f"{key}: string (max {cap} chars)")
    if c.get("max") is not None and not (_finite(c["max"]) and c["max"] > 0):
        errs.append("max: positive number")
    return errs


def validate_components(components: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(components, list) or not components:
        return ["components: must be a non-empty array"]
    if len(components) > MAX_COMPONENTS:
        return [f"components: too many (max {MAX_COMPONENTS})"]
    ids: set[str] = set()
    for i, c in enumerate(components):
        if not isinstance(c, dict):
            errors.append(f"components[{i}]: must be an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"components[{i}].id: required")
            continue
        if cid in ids:
            errors.append(f"components[{i}].id: duplicate id {cid!r}")
        ids.add(cid)
        name = c.get("component")
        if name not in REQUIRED:
            errors.append(
                f"components[{i}].component: {name!r} is not in the Softnix catalog (allowed: {', '.join(ALLOWED)})"
            )
            continue
        _check_component(i, c, errors)
    if "root" not in ids:
        errors.append('components: one component must have id "root"')
    for i, c in enumerate(components):
        if not isinstance(c, dict):
            continue
        refs = ([c["child"]] if isinstance(c.get("child"), str) else []) + (
            c["children"] if isinstance(c.get("children"), list) else []
        )
        for r in refs:
            if not isinstance(r, str) or r not in ids:
                errors.append(f"components[{i}]({c.get('id')!s}): references missing child {r!r}")
    return errors


def normalize(payload: Any, fallback_surface_id: str = "surface") -> tuple[list[dict] | None, list[str]]:
    """Return (canonical v0.9 message list, errors)."""
    if isinstance(payload, str):
        if len(payload) > MAX_PAYLOAD_CHARS:
            return None, ["payload: too large"]
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, [f"payload: invalid JSON ({exc.msg} at line {exc.lineno} col {exc.colno})"]
    errors: list[str] = []
    _walk(payload, 0, "$", errors)
    if errors:
        return None, errors
    surface_id = fallback_surface_id
    components: Any = None
    data: Any = None
    messages = (
        payload
        if isinstance(payload, list)
        else payload.get("messages")
        if isinstance(payload, dict)
        else None
    )
    if isinstance(messages, list):
        for j, m in enumerate(messages):
            if not isinstance(m, dict):
                return None, [f"messages[{j}]: must be an object"]
            cs = m.get("createSurface")
            if isinstance(cs, dict) and isinstance(cs.get("surfaceId"), str):
                surface_id = cs["surfaceId"]
            uc = m.get("updateComponents")
            if isinstance(uc, dict):
                comps = uc.get("components")
                components = (
                    (components + comps)
                    if isinstance(components, list) and isinstance(comps, list)
                    else comps
                )
            ud = m.get("updateDataModel")
            if isinstance(ud, dict):
                if ud.get("path") not in (None, "", "/"):
                    return None, [f'messages[{j}].updateDataModel.path: must be "/"']
                data = ud.get("value")
            if "deleteSurface" in m:
                return None, [f"messages[{j}]: deleteSurface is not supported"]
    elif isinstance(payload, dict) and isinstance(payload.get("components"), list):
        surface_id = payload.get("surfaceId") if isinstance(payload.get("surfaceId"), str) else surface_id
        components = payload["components"]
        data = payload.get("data", payload.get("dataModel"))
    else:
        return None, ['payload: expected an A2UI v0.9 message list or {"components": [...]}']
    errors = validate_components(components)
    if data is not None and not isinstance(data, dict):
        errors.append("updateDataModel.value: must be an object")
    if errors:
        return None, errors
    surface_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", surface_id)[:64] or fallback_surface_id
    out: list[dict] = [
        {
            "version": A2UI_VERSION,
            "createSurface": {"surfaceId": surface_id, "catalogId": SOFTNIX_CATALOG_ID},
        },
        {"version": A2UI_VERSION, "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]
    if data is not None:
        out.append(
            {
                "version": A2UI_VERSION,
                "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": data},
            }
        )
    return out, []
