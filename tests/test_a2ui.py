"""A2UI POC: payload gate, present_ui retries/artifacts, OpenRouter price math, flag."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from claw import a2ui
from claw.a2ui import SOFTNIX_CATALOG_ID, price_watch
from claw.a2ui.validate import normalize
from claw.core.builtin_skills import builtin_skills, get_builtin_skill
from claw.core.loop import visible_artifacts
from claw.tools.a2ui import SNAPSHOT_REL, OpenRouterPriceWatchTool, PresentUiTool

CLARIFY = [
    {"version": "v0.9", "createSurface": {"surfaceId": "c1", "catalogId": "whatever"}},
    {
        "version": "v0.9",
        "updateComponents": {
            "surfaceId": "c1",
            "components": [
                {"id": "root", "component": "Card", "child": "col"},
                {"id": "col", "component": "Column", "children": ["q", "pick", "go"]},
                {"id": "q", "component": "Text", "text": "อยากได้รายงานแบบไหน"},
                {
                    "id": "pick",
                    "component": "ChoicePicker",
                    "options": [
                        {"label": "ทางการ", "value": "ทางการ"},
                        {"label": "ไม่ทางการ", "value": "ไม่ทางการ"},
                    ],
                    "value": {"path": "/style"},
                },
                {
                    "id": "go",
                    "component": "Button",
                    "child": "go_t",
                    "action": {"event": {"name": "brief", "context": {"style": {"path": "/style"}}}},
                },
                {"id": "go_t", "component": "Text", "text": "ไปต่อ"},
            ],
        },
    },
    {"version": "v0.9", "updateDataModel": {"surfaceId": "c1", "path": "/", "value": {"style": []}}},
]


@pytest.fixture(autouse=True)
def _flag_on():
    a2ui.configure(True)
    yield
    a2ui.configure(True)


def test_valid_payload_is_pinned_to_softnix_catalog():
    msgs, errors = normalize(json.dumps(CLARIFY))
    assert errors == []
    assert msgs[0]["createSurface"]["catalogId"] == SOFTNIX_CATALOG_ID
    assert [next(iter(k for k in m if k != "version")) for m in msgs] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]


def test_compact_form_normalizes():
    msgs, errors = normalize(
        {"components": CLARIFY[1]["updateComponents"]["components"], "data": {"style": []}}
    )
    assert errors == [] and len(msgs) == 3


@pytest.mark.parametrize(
    "payload,needle",
    [
        ("{not json", "invalid JSON"),
        (
            [
                {
                    "version": "v0.9",
                    "updateComponents": {
                        "surfaceId": "x",
                        "components": [{"id": "root", "component": "Script", "src": "x"}],
                    },
                }
            ],
            "not in the Softnix catalog",
        ),
        ({"components": [{"id": "root", "component": "Card", "child": "nope"}]}, "missing child"),
        ({"components": [{"id": "a", "component": "Text", "text": "x"}]}, 'id "root"'),
        (
            {
                "components": [
                    {
                        "id": "root",
                        "component": "Button",
                        "child": "t",
                        "action": {
                            "functionCall": {"call": "openUrl", "args": {"url": "javascript:alert(1)"}}
                        },
                    },
                    {"id": "t", "component": "Text", "text": "x"},
                ]
            },
            "functionCall",
        ),
        ({"components": [{"id": "root", "component": "Text", "text": "x" * 5000}]}, "longer than"),
        (
            {"components": [{"id": "root", "component": "Text", "text": "x", "__proto__": {"polluted": 1}}]},
            "forbidden key",
        ),
        (
            {"components": [{"id": "root", "component": "DeltaStat", "label": "x", "value": "12%"}]},
            "must be a number",
        ),
    ],
)
def test_invalid_payloads_are_rejected_with_paths(payload, needle):
    msgs, errors = normalize(payload)
    assert msgs is None
    assert any(needle in e for e in errors), errors


def test_markup_in_text_is_kept_as_data_not_rejected():
    # Rendering is plain text (React text nodes), so markup is harmless data.
    xss = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    msgs, errors = normalize({"components": [{"id": "root", "component": "Text", "text": xss}]})
    assert errors == []
    assert msgs[1]["updateComponents"]["components"][0]["text"] == xss


def test_a2ui_artifact_is_not_hidden_but_json_would_be():
    assert visible_artifacts(["ui/card.a2ui"]) == ["ui/card.a2ui"]
    assert visible_artifacts(["ui/card.a2ui.json"]) == []  # why the suffix is .a2ui


async def test_present_ui_writes_artifact(tmp_path: Path):
    tool = PresentUiTool(tmp_path)
    out = await tool.execute(messages=CLARIFY, title="report-clarify")
    assert out.startswith("OK.")
    files = list((tmp_path / "ui").glob("report-clarify-*.a2ui"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved[0]["createSurface"]["catalogId"] == SOFTNIX_CATALOG_ID


async def test_present_ui_two_retries_then_text_fallback(tmp_path: Path):
    tool = PresentUiTool(tmp_path)
    bad = {"components": [{"id": "root", "component": "Marquee"}]}
    first = await tool.execute(messages=bad)
    second = await tool.execute(messages=bad)
    third = await tool.execute(messages=bad)
    assert first.startswith("Error: VALIDATION_FAILED (attempt 1/3)")
    assert second.startswith("Error: VALIDATION_FAILED (attempt 2/3)")
    assert "Do NOT call present_ui again" in third and "in the user's language" in third
    assert not (tmp_path / "ui").exists()
    # the counter resets afterwards, and a success resets it too
    assert (await tool.execute(messages=bad)).startswith("Error: VALIDATION_FAILED (attempt 1/3)")
    assert (await tool.execute(messages=CLARIFY)).startswith("OK.")
    assert (await tool.execute(messages=bad)).startswith("Error: VALIDATION_FAILED (attempt 1/3)")


def test_flag_gates_skill():
    assert get_builtin_skill("a2ui-cards") is not None
    a2ui.configure(False)
    assert get_builtin_skill("a2ui-cards") is None
    assert all(s.name != "a2ui-cards" for s in builtin_skills())


# ---- price math --------------------------------------------------------------

NOW = 1_800_000_000
DAY = 86_400


def _api(*models):
    return {
        "data": [
            {"id": i, "name": n, "created": c, "pricing": {"prompt": p, "completion": q}}
            for (i, n, c, p, q) in models
        ]
    }


API = _api(
    ("acme/old-1", "Acme Old", NOW - 90 * DAY, "0.000003", "0.000015"),
    ("acme/new-2", "Acme New", NOW - 2 * DAY, "0.0000025", "0.00001"),
    ("acme/new-2:free", "Acme New free", NOW - 2 * DAY, "0", "0"),
    ("solo/first", "Solo First", NOW - 1 * DAY, "0.0000001", "0.0000004"),
    ("router/auto", "Auto", NOW - 1 * DAY, "-1", "-1"),
    ("acme/old-1:batch", "Acme Old batch", NOW - 80 * DAY, "0.0000001", "0.0000001"),
)


def test_per_million_is_exact_decimal():
    assert price_watch.per_million("0.000003") == Decimal("3.0000")
    assert price_watch.per_million("0.00000015") == Decimal("0.1500")
    assert price_watch.per_million("-1") is None
    assert price_watch.per_million("abc") is None


def test_first_run_compares_with_previous_model_or_marks_new():
    models = price_watch.parse_models(API)
    assert {m.id for m in models} == {
        "acme/old-1",
        "acme/new-2",
        "acme/new-2:free",
        "solo/first",
        "acme/old-1:batch",
    }
    rows = {r.model.id: r for r in price_watch.compute(models, None, NOW, 7)}
    assert set(rows) == {"acme/new-2", "acme/new-2:free", "solo/first"}
    new2 = rows["acme/new-2"]
    # the newer ':batch' variant is NOT the baseline of a regular model
    assert new2.basis == "prev_model" and new2.baseline_label.endswith("acme/old-1")
    assert (new2.dir_input, new2.pct_input) == ("down", Decimal("-16.7"))  # 2.5 vs 3.0
    assert (new2.dir_output, new2.pct_output) == ("down", Decimal("-33.3"))  # 10 vs 15
    assert rows["solo/first"].basis == "none" and rows["solo/first"].dir_output == "new"
    assert rows["acme/new-2:free"].basis == "none"  # never compared with a paid model


def test_second_run_compares_with_snapshot():
    models = price_watch.parse_models(API)
    snap = price_watch.snapshot_of(models, NOW - 3600)
    snap["models"]["acme/new-2"]["output"] = "8.0000"  # it got more expensive since
    rows = {r.model.id: r for r in price_watch.compute(models, snap, NOW, 7)}
    r = rows["acme/new-2"]
    assert r.basis == "snapshot"
    assert (r.dir_output, r.pct_output) == ("up", Decimal("25.0"))
    assert (r.dir_input, r.pct_input) == ("flat", Decimal("0.0"))


async def test_price_watch_tool_writes_card_and_snapshot(tmp_path: Path):
    async def fake_fetch():
        return API

    tool = OpenRouterPriceWatchTool(tmp_path, fetch=fake_fetch)
    out = await tool.execute(days=7)
    assert out.startswith("OK.") and "acme/new-2 | 2.5 | 10 | ▼ -16.7% | ▼ -33.3%" in out
    cards = list((tmp_path / "ui").glob("openrouter-prices-*.a2ui"))
    assert len(cards) == 1
    msgs = json.loads(cards[0].read_text(encoding="utf-8"))
    canonical, errors = normalize(msgs)
    assert errors == [], errors
    comps = {c["id"]: c for c in msgs[1]["updateComponents"]["components"]}
    assert comps["chart"]["items"][0] == {"label": "solo/first", "a": 0.1, "b": 0.4}
    assert (tmp_path / SNAPSHOT_REL).exists()
    # second run: same prices -> flat vs the snapshot just stored
    out2 = await tool.execute(days=7)
    assert "acme/new-2 | 2.5 | 10 | ■ 0.0% | ■ 0.0% | เทียบกับข้อมูลที่บันทึกไว้" in out2


async def test_price_watch_fetch_failure_is_reported(tmp_path: Path):
    async def boom():
        raise RuntimeError("down")

    out = await OpenRouterPriceWatchTool(tmp_path, fetch=boom).execute()
    assert out.startswith("Error:") and "do not guess" in out


async def _token(c, email="a2ui@example.com"):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.parametrize("enabled", [True, False])
async def test_features_endpoint_reports_flag(db_factory, enabled):
    from tests.conftest_app import build_api_app, client

    app = build_api_app(db_factory, a2ui_enabled=enabled)
    async with client(app) as c:
        tok = await _token(c)
        r = await c.get("/api/features", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["a2ui"] is enabled


async def test_loop_surfaces_present_ui_card_as_artifact(tmp_path: Path):
    """End to end through the real AgentLoop: the card file reaches the turn's
    artifacts (so the chat renders and persists it); the snapshot dot-dir does not."""
    from claw.core.loop import AgentLoop
    from claw.providers.base import ChatResult, ToolCall
    from claw.tools.registry import ToolRegistry
    from tests.conftest import FakeProvider, text_turn

    async def fake_fetch():
        return API

    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="present_ui",
                            arguments={"messages": CLARIFY, "title": "report-clarify"},
                        )
                    ],
                )
            ],
            [
                ChatResult(
                    content=None, tool_calls=[ToolCall(id="c2", name="openrouter_price_watch", arguments={})]
                )
            ],
            text_turn("เลือกรูปแบบในการ์ดได้เลยครับ"),
        ]
    )
    tools = ToolRegistry()
    tools.register(PresentUiTool(tmp_path))
    tools.register(OpenRouterPriceWatchTool(tmp_path, fetch=fake_fetch))
    loop = AgentLoop(provider, tools, workspace=tmp_path)

    def emit(_event):
        return None

    outcome = await loop.run_turn("t1", [{"role": "user", "content": "สร้างรายงาน"}], emit)
    assert len(outcome.artifacts) == 2, outcome.artifacts
    assert all(a.startswith("ui/") and a.endswith(".a2ui") for a in outcome.artifacts)
    assert (tmp_path / SNAPSHOT_REL).exists() and not any(".a2ui-state" in a for a in outcome.artifacts)


THAI = __import__("re").compile(r"[\u0e00-\u0e7f]")


def _card_strings(msgs) -> str:
    comps = msgs[1]["updateComponents"]["components"]
    out = []
    for c in comps:
        for k in ("text", "title", "label", "baseline"):
            if isinstance(c.get(k), str):
                out.append(c[k])
        for col in c.get("columns", []):
            out.append(col["label"])
        for row in c.get("rows", []):
            out += [v for v in row.values() if isinstance(v, str)]
    return " ".join(out)


async def test_price_watch_card_follows_lang_en(tmp_path: Path):
    async def fake_fetch():
        return API

    tool = OpenRouterPriceWatchTool(tmp_path, fetch=fake_fetch)
    assert tool.parameters["properties"]["lang"]["enum"] == ["th", "en"]
    out = await tool.execute(days=7, lang="en")
    msgs = json.loads(next((tmp_path / "ui").glob("openrouter-prices-*.a2ui")).read_text(encoding="utf-8"))
    assert normalize(msgs)[1] == []
    strings = _card_strings(msgs)
    assert not THAI.search(strings), strings
    assert "New models on OpenRouter (last 7 days)" in strings and "vs previous acme model: acme/old-1" in strings
    # numbers are identical whatever the language
    assert "acme/new-2 | 2.5 | 10 | ▼ -16.7% | ▼ -33.3%" in out and not THAI.search(out.split("\n", 4)[-1])
    assert "language of the user's latest message" in out


async def test_price_watch_card_defaults_to_thai_and_unknown_lang_is_english(tmp_path: Path):
    async def fake_fetch():
        return API

    await OpenRouterPriceWatchTool(tmp_path / "a", fetch=fake_fetch).execute(days=7)
    th = json.loads(next((tmp_path / "a" / "ui").glob("*.a2ui")).read_text(encoding="utf-8"))
    assert "ราคาโมเดลใหม่บน OpenRouter" in _card_strings(th)
    # Thai card has no stray English words beyond names/units/source URL
    import re as _re
    words = set(_re.findall(r"[A-Za-z]+", _card_strings(th)))
    assert words <= {"OpenRouter", "openrouter", "ai", "api", "v", "models", "USD", "ICT", "M", "Acme", "New", "Solo",
                     "First", "Old", "batch", "free", "acme", "new", "solo", "first", "old"}, words
    await OpenRouterPriceWatchTool(tmp_path / "b", fetch=fake_fetch).execute(days=7, lang="ja")
    other = json.loads(next((tmp_path / "b" / "ui").glob("*.a2ui")).read_text(encoding="utf-8"))
    assert not THAI.search(_card_strings(other))


def test_language_rule_in_skill_and_tool_descriptions(tmp_path: Path):
    from claw.a2ui.skill import A2UI_SKILL_CONTENT

    assert "## Language" in A2UI_SKILL_CONTENT and "การ์ด" in A2UI_SKILL_CONTENT
    assert "short Thai labels" not in A2UI_SKILL_CONTENT
    assert "language of the user's latest message" in PresentUiTool(tmp_path).description
    assert "`lang`" in OpenRouterPriceWatchTool(tmp_path).description


async def test_present_ui_ok_reminds_reply_language(tmp_path: Path):
    out = await PresentUiTool(tmp_path).execute(messages=CLARIFY)
    assert out.startswith("OK.") and "user's language only" in out and "การ์ด" in out


def test_card_reply_prefixes_documented_for_both_languages(tmp_path: Path):
    from claw.a2ui.skill import A2UI_SKILL_CONTENT

    for prefix in ("[การ์ด]", "[Card]"):
        assert prefix in A2UI_SKILL_CONTENT
        assert prefix in PresentUiTool(tmp_path).description
