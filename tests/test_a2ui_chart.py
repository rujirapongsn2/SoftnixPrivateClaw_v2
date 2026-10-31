"""A2UI Chart: server-side aggregation (Decimal), deltas, limits, card validity, tool retries."""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from claw.a2ui import SOFTNIX_CATALOG_ID
from claw.a2ui import chart_data as cd
from claw.a2ui.skill import A2UI_SKILL_CONTENT
from claw.a2ui.validate import normalize, validate_chart
from claw.core.loop import visible_artifacts
from claw.tools.a2ui import ChartFromDataTool

SALES = [
    {"date": "2026-01-05", "region": "North", "amount": "1,000.10"},
    {"date": "2026-01-20", "region": "South", "amount": "200.20"},
    {"date": "2026-02-03", "region": "North", "amount": "1,500.00"},
    {"date": "2026-02-14", "region": "South", "amount": "(100)"},
    {"date": "2026-03-01", "region": "North", "amount": "฿900"},
    {"date": "2026-03-09", "region": "South", "amount": ""},
    {"date": "bad", "region": "North", "amount": "5"},
]


def spec(**kw):
    kw.setdefault("chart_type", "line")
    kw.setdefault("x", "date")
    kw.setdefault("y", ["amount"])
    return cd.ChartSpec(**kw)


def test_parse_number_handles_commas_currency_parens_percent():
    assert cd.parse_number("1,234.50") == Decimal("1234.50")
    assert cd.parse_number("฿ 1,000") == Decimal("1000")
    assert cd.parse_number("(12.5)") == Decimal("-12.5")
    assert cd.parse_number("15%") == Decimal("15")
    assert cd.parse_number(0.1) == Decimal("0.1")
    assert cd.parse_number("") is None and cd.parse_number(None) is None
    with pytest.raises(ValueError):
        cd.parse_number("abc")


def test_monthly_sum_is_exact_decimal_with_deltas_th():
    res = cd.aggregate(SALES, spec(x_grain="month", delta="previous", lang="th"))
    assert res.categories == ["ม.ค. 2026", "ก.พ. 2026", "มี.ค. 2026"]
    (_, vals), = res.series
    assert vals == [Decimal("1200.30"), Decimal("1400.00"), Decimal("900.00")]
    assert res.deltas[0][0] is None
    assert res.deltas[0][1] == {"pct": 16.6, "direction": "up"}  # 199.70/1200.30 = 16.637..%
    assert res.deltas[0][2] == {"pct": -35.7, "direction": "down"}  # -500/1400 = -35.714..%
    assert res.rows_used == 5 and res.skipped == 2 and res.rows_total == 7


def test_english_month_labels_and_avg_count():
    res = cd.aggregate(SALES, spec(x_grain="month", lang="en", agg="avg"))
    assert res.categories == ["Jan 2026", "Feb 2026", "Mar 2026"]
    assert res.series[0][1] == [Decimal("600.15"), Decimal("700.00"), Decimal("900.00")]
    cnt = cd.aggregate(SALES, spec(chart_type="bar", x="region", y=[], agg="count", lang="en"))
    assert cnt.categories == ["North", "South"] and cnt.series[0][1] == [Decimal(4), Decimal(3)]
    assert cnt.decimals == 0


def test_series_by_pivots_for_stacked_and_fills_gaps_with_null():
    res = cd.aggregate(SALES, spec(chart_type="stackedBar", x_grain="month", series_by="region"))
    labels = [s for s, _ in res.series]
    assert labels == ["North", "South"]
    south = dict(res.series)["South"]
    assert south == [Decimal("200.20"), Decimal("-100.00"), None]


def test_pie_top_11_plus_other_and_sorted_desc():
    rows = [{"k": f"c{i:02d}", "v": i} for i in range(1, 16)]
    res = cd.aggregate(rows, spec(chart_type="pie", x="k", y=["v"], lang="en"))
    assert len(res.categories) == 12 and res.categories[-1] == "Other"
    assert res.categories[0] == "c15"
    assert res.series[0][1][-1] == Decimal(sum(range(1, 5)))  # c01..c04 merged
    assert res.merged_other == 4
    th = cd.aggregate(rows, spec(chart_type="donut", x="k", y=["v"], lang="th"))
    assert th.categories[-1] == "อื่น ๆ"


def test_too_many_categories_asks_for_limit_or_grain():
    rows = [{"d": f"2026-01-{i:02d}", "v": 1} for i in range(1, 32)] * 3
    rows += [{"d": f"2026-02-{i:02d}", "v": 1} for i in range(1, 29)] + [{"d": f"2026-03-{i:02d}", "v": 1} for i in range(1, 10)]
    with pytest.raises(cd.ChartDataError, match="limit"):
        cd.aggregate(rows, spec(chart_type="bar", x="d", y=["v"]))
    ok = cd.aggregate(rows, spec(chart_type="bar", x="d", y=["v"], x_grain="month"))
    assert ok.series[0][1] == [Decimal(93), Decimal(28), Decimal(9)]


def test_unknown_column_lists_columns_and_case_insensitive_match():
    with pytest.raises(cd.ChartDataError, match="columns are"):
        cd.aggregate(SALES, spec(y=["revenue"]))
    res = cd.aggregate(SALES, spec(chart_type="bar", x="REGION", y=["Amount"]))
    assert res.categories == ["North", "South"]


@pytest.mark.parametrize(
    "kw, needle",
    [
        ({"chart_type": "radar"}, "chart_type"),
        ({"agg": "median"}, "agg"),
        ({"chart_type": "pie", "y": ["a", "b"]}, "pie"),
        ({"series_by": "region", "y": ["a", "b"]}, "series_by"),
        ({"title": "x" * 201}, "title"),
        ({"limit": 99}, "limit"),
    ],
)
def test_spec_validation(kw, needle):
    with pytest.raises(cd.ChartDataError, match=needle):
        cd.validate_spec(spec(**kw))


def test_card_is_valid_and_pinned_and_labels_stay_plain_text():
    xss = '<img src=x onerror=alert(1)>'
    rows = [{"k": xss, "v": 1}, {"k": "b", "v": 2}]
    s = spec(chart_type="bar", x="k", y=["v"], title=xss, delta="previous")
    res = cd.aggregate(rows, s)
    msgs = cd.build_messages(s, res, "data.csv")
    canonical, errors = normalize(msgs)
    assert errors == [] and canonical[0]["createSurface"]["catalogId"] == SOFTNIX_CATALOG_ID
    chart = next(c for c in canonical[1]["updateComponents"]["components"] if c["component"] == "Chart")
    assert chart["categories"][0] == xss  # data, rendered as SVG text by React (escaped)
    assert chart["series"][0]["deltas"][1] == {"pct": 100.0, "direction": "up"}


@pytest.mark.parametrize(
    "chart, needle",
    [
        ({"chartType": "radar", "categories": ["a"], "series": [{"label": "s", "values": [1]}]}, "chartType"),
        ({"chartType": "bar", "categories": [], "series": [{"label": "s", "values": []}]}, "categories"),
        ({"chartType": "bar", "categories": ["a", "b"], "series": [{"label": "s", "values": [1]}]}, "values"),
        ({"chartType": "bar", "categories": ["a"], "series": [{"label": "s", "values": ["1"]}]}, "numbers"),
        ({"chartType": "pie", "categories": ["a"], "series": [{"label": "s", "values": [-1]}]}, ">= 0"),
        (
            {"chartType": "pie", "categories": ["a"], "series": [{"label": "s", "values": [1]}, {"label": "t", "values": [1]}]},
            "exactly one",
        ),
        ({"chartType": "bar", "categories": ["a" * 61], "series": [{"label": "s", "values": [1]}]}, "categories"),
        (
            {"chartType": "bar", "categories": ["a"], "series": [{"label": "s", "values": [1], "deltas": [{"pct": 1, "direction": "sideways"}]}]},
            "deltas",
        ),
        ({"chartType": "line", "categories": ["a"], "series": [{"label": str(j), "values": [1]} for j in range(9)]}, "series"),
    ],
)
def test_validate_chart_rejects(chart, needle):
    assert any(needle in e for e in validate_chart(chart)), validate_chart(chart)


def test_validate_chart_accepts_the_limits():
    full = {"chartType": "line", "categories": [str(i) for i in range(60)], "series": [{"label": str(j), "values": [1] * 60} for j in range(8)]}
    assert validate_chart(full) == []
    assert validate_chart({"chartType": "donut", "categories": ["a", "b"], "series": [{"label": "s", "values": [0, None]}]}) == []


def _write_csv(path: Path, rows: list[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


async def test_tool_reads_csv_xlsx_json_and_writes_visible_artifact(tmp_path: Path):
    from openpyxl import Workbook

    _write_csv(tmp_path / "sales.csv", SALES)
    wb = Workbook()
    ws = wb.active
    ws.append(["dept", "expense"])
    for row in (("IT", 1200.5), ("HR", 300), ("IT", 99.5), ("Ops", None)):
        ws.append(row)
    wb.save(tmp_path / "exp.xlsx")
    (tmp_path / "t.json").write_text(json.dumps({"rows": [{"model": "a", "tok": 5}, {"model": "b", "tok": 7}]}))
    tool = ChartFromDataTool(tmp_path)

    out = await tool.execute(source="/workspace/sales.csv", chart_type="line", x="date", y="amount", x_grain="month", delta="previous", lang="en")
    assert out.startswith("OK.") and "1,200.30" in out and "up +16.6%" in out
    out = await tool.execute(source="exp.xlsx", chart_type="pie", x="dept", y="expense", lang="th")
    assert "IT | 1,300" in out and "HR | 300" in out  # whole totals -> 0 decimals
    out = await tool.execute(source="t.json", chart_type="hbar", x="model", y=["tok"], decimals=0)
    assert "b | 7" in out
    out = await tool.execute(rows=[{"x": "a", "y": 1}], chart_type="bar", x="x", y="y")
    assert out.startswith("OK.")

    files = sorted((tmp_path / "ui").glob("chart-*.a2ui"))
    assert len(files) == 4
    rels = [f.relative_to(tmp_path).as_posix() for f in files]
    assert visible_artifacts(rels) == rels
    for f in files:
        _, errors = normalize(json.loads(f.read_text(encoding="utf-8")))
        assert errors == []


async def test_tool_refuses_paths_outside_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_csv(tmp_path / "secret.csv", SALES)
    out = await ChartFromDataTool(ws).execute(source="../secret.csv", chart_type="bar", x="region", y="amount")
    assert "inside the workspace" in out
    assert not (ws / "ui").exists()


async def test_tool_two_retries_then_text_fallback(tmp_path: Path):
    _write_csv(tmp_path / "s.csv", SALES)
    tool = ChartFromDataTool(tmp_path)
    bad = dict(source="s.csv", chart_type="bar", x="region", y="nope")
    first, second, third = [await tool.execute(**bad) for _ in range(3)]
    assert first.startswith("Error: CHART_FAILED (attempt 1/3)") and "columns are" in first
    assert second.startswith("Error: CHART_FAILED (attempt 2/3)")
    assert "Do NOT call chart_from_data again" in third and "Do not invent numbers" in third
    assert (await tool.execute(source="s.csv", chart_type="bar", x="region", y="amount")).startswith("OK.")
    assert (await tool.execute(**bad)).startswith("Error: CHART_FAILED (attempt 1/3)")


async def test_tool_rejects_unsupported_file(tmp_path: Path):
    (tmp_path / "x.exe").write_bytes(b"MZ")
    out = await ChartFromDataTool(tmp_path).execute(source="x.exe", chart_type="bar", x="a", y="b")
    assert "unsupported file type" in out


def test_skill_documents_chart_tool():
    assert "chart_from_data" in A2UI_SKILL_CONTENT and "good_direction" in A2UI_SKILL_CONTENT
    assert "written only by chart_from_data" in A2UI_SKILL_CONTENT


async def test_missing_source_error_lists_data_files(tmp_path: Path):
    (tmp_path / "data").mkdir()
    _write_csv(tmp_path / "data" / "sales.csv", SALES)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.csv").write_text("a\n1")
    out = await ChartFromDataTool(tmp_path).execute(chart_type="bar", x="region", y="amount")
    assert "`source` is missing" in out and "data/sales.csv" in out and ".hidden" not in out
    assert "source" in ChartFromDataTool.parameters["required"]


def test_whole_numbers_default_to_zero_decimals():
    rows = [{"m": "a", "tok": 5}, {"m": "b", "tok": 7}]
    assert cd.aggregate(rows, spec(chart_type="bar", x="m", y=["tok"])).decimals == 0
    assert cd.aggregate(SALES, spec(chart_type="bar", x="region")).decimals == 2


async def test_missing_y_uses_the_only_numeric_column_else_errors(tmp_path: Path):
    _write_csv(tmp_path / "s.csv", SALES)
    tool = ChartFromDataTool(tmp_path)
    out = await tool.execute(source="s.csv", chart_type="bar", x="region", lang="en")
    assert out.startswith("OK.") and "used the only numeric column 'amount'" in out
    two = [{"k": "a", "p": 1, "q": 2}]
    with pytest.raises(cd.ChartDataError, match=r"numeric columns: \['p', 'q'\]"):
        cd.aggregate(two, spec(chart_type="bar", x="k", y=[]))
    out = await tool.execute(source="s.csv", chart_type="bar", x="region", agg="count", lang="en")
    assert out.startswith("OK.") and "North | 4" in out and "South | 3" in out


def test_count_counts_non_empty_cells_of_any_column():
    res = cd.aggregate(SALES, spec(chart_type="bar", x="region", y=["date"], agg="count"))
    assert res.series[0][1] == [Decimal(4), Decimal(3)]  # dates are not numbers, still counted
    assert res.series[0][0] == "จำนวน"  # a count is labelled as a count, not as the column
    assert cd.aggregate(SALES, spec(chart_type="bar", x="region", y=["date"], agg="count", lang="en")).series[0][0] == "Count"


async def test_present_ui_rejects_hand_typed_chart(tmp_path: Path):
    from claw.tools.a2ui import PresentUiTool

    card = {"components": [{"id": "root", "component": "Chart", "chartType": "bar", "categories": ["a"], "series": [{"label": "s", "values": [1]}]}]}
    out = await PresentUiTool(tmp_path).execute(messages=card)
    assert out.startswith("Error: VALIDATION_FAILED") and "chart_from_data" in out
    assert not (tmp_path / "ui").exists()
