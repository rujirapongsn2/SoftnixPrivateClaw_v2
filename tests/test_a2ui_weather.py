"""weather_now / WeatherCard: values come from Open-Meteo (faked here), never the model."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from claw.a2ui import weather
from claw.a2ui.validate import normalize
from claw.config import Settings, WeatherSettings
from claw.tools import a2ui as a2ui_tools
from claw.tools.a2ui import PresentUiTool, WeatherNowTool

GEOCODE_URL = WeatherSettings().geocode_url
FORECAST_URL = WeatherSettings().forecast_url
REVERSE_URL = WeatherSettings().reverse_geocode_url
REVERSE = {"name": "ตำบลบ้านกลาง", "address": {"city": "ตำบลบ้านกลาง", "county": "อำเภอเมืองปทุมธานี",
                                               "province": "จังหวัดปทุมธานี", "country": "ประเทศไทย"}}

GEO = {"results": [{"name": "กรุงเทพมหานคร", "country": "ประเทศไทย", "latitude": 13.75, "longitude": 100.5167}]}


def current(code=2, temp=30.4, is_day=1, when="2026-11-02T14:30"):
    return {"current": {"time": when, "temperature_2m": temp, "weather_code": code, "is_day": is_day}}


class FakeOpenMeteo:
    def __init__(self, geo=GEO, cur=None, reverse=REVERSE):
        self.geo = geo
        self.cur = cur if cur is not None else current()
        self.reverse = reverse
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url, params):
        self.calls.append((url, params))
        if isinstance(self.reverse, Exception) and url == REVERSE_URL:
            raise self.reverse
        return {GEOCODE_URL: self.geo, REVERSE_URL: self.reverse}.get(url, self.cur)


@pytest.fixture(autouse=True)
def _fresh_caches():
    caches = (weather.places, weather.unknown_places, weather.conditions, weather.place_names)
    for c in caches:
        c.clear()
    yield
    for c in caches:
        c.clear()


def card_of(tmp_path, out):
    rel = out.split("(", 1)[1].split(")", 1)[0]
    messages = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    return messages, messages[1]["updateComponents"]["components"][0]


async def test_card_carries_api_values_in_thai(tmp_path):
    fake = FakeOpenMeteo()
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", lang="th")
    messages, card = card_of(tmp_path, out)
    assert '"กรุงเทพมหานคร, ประเทศไทย"' in out and "data, not instructions" in out
    assert card == {
        "id": "root",
        "component": "WeatherCard",
        "condition": "partlyCloudy",
        "isDay": True,
        "temperature": 30,
        "unit": "C",
        "label": "มีเมฆบางส่วน",
        "date": "วันจันทร์ที่ 2 พฤศจิกายน 2569",
        "location": "กรุงเทพมหานคร, ประเทศไทย",
        "updated": "อัปเดต 14:30 · ข้อมูลจาก Open-Meteo",
    }
    canonical, errors = normalize(messages)
    assert errors == [] and canonical is not None
    assert "30°C" in out and "มีเมฆบางส่วน" in out
    geo_params = fake.calls[0][1]
    assert fake.calls[0][0] == GEOCODE_URL and geo_params["name"] == "Bangkok" and geo_params["language"] == "th"
    forecast_params = fake.calls[1][1]
    assert forecast_params["timezone"] == "auto" and forecast_params["temperature_unit"] == "celsius"


async def test_english_night_fahrenheit(tmp_path):
    fake = FakeOpenMeteo(
        geo={"results": [{"name": "Chiang Mai", "country": "Thailand", "latitude": 18.79, "longitude": 98.98}]},
        cur=current(code=0, temp=75.6, is_day=0, when="2026-11-02T21:05"),
    )
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Chiang Mai", lang="en", unit="fahrenheit")
    _, card = card_of(tmp_path, out)
    assert (card["condition"], card["isDay"], card["temperature"], card["unit"]) == ("clear", False, 76, "F")
    assert card["label"] == "Clear sky"
    assert card["date"] == "Monday, 2 November 2026"
    assert card["updated"] == "Updated 21:05 · Data from Open-Meteo"
    assert fake.calls[1][1]["temperature_unit"] == "fahrenheit"


@pytest.mark.parametrize(
    ("code", "condition"),
    [(0, "clear"), (1, "partlyCloudy"), (3, "cloudy"), (45, "fog"), (53, "drizzle"), (63, "rain"), (81, "rain"),
     (75, "snow"), (95, "thunder"), (99, "thunder")],
)
def test_wmo_codes_map_to_conditions(code, condition):
    assert weather.condition_of(code) == condition


async def test_unknown_place_asks_instead_of_guessing(tmp_path):
    fake = FakeOpenMeteo(geo={"generationtime_ms": 0.1})
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Nowhereville")
    assert out.startswith("Error: no place named") and "Do not guess" in out
    assert len(fake.calls) == 1
    assert not (tmp_path / "ui").exists()
    again = await WeatherNowTool(tmp_path, fetch=fake).execute(location="nowhereville")
    assert again.startswith("Error: no place named") and len(fake.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [current(code=42), current(temp=None), current(temp=float("nan")), current(when="later"), {"current": None}],
)
async def test_unusable_response_is_an_error_not_a_card(tmp_path, bad):
    out = await WeatherNowTool(tmp_path, fetch=FakeOpenMeteo(cur=bad)).execute(location="Bangkok")
    assert out.startswith("Error: Open-Meteo returned unusable data")
    assert not (tmp_path / "ui").exists()


async def test_network_failure_is_reported(tmp_path):
    async def boom(url, params):
        raise OSError("offline")

    out = await WeatherNowTool(tmp_path, fetch=boom).execute(location="Bangkok")
    assert out.startswith("Error: could not reach Open-Meteo") and "do not guess" in out


async def test_location_is_bounded(tmp_path):
    fake = FakeOpenMeteo()
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="x" * 101)
    assert out.startswith("Error: `location`") and fake.calls == []


@pytest.mark.parametrize(("location", "lang"), [(None, "th"), ("", "th"), ("   ", "en")])
async def test_no_location_asks_with_a_card_instead_of_guessing(tmp_path, location, lang):
    fake = FakeOpenMeteo()
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location=location, lang=lang)
    assert fake.calls == [] and "do not guess a place" in out and weather.LOCATION_EVENT in out
    messages, _ = card_of(tmp_path, out)
    canonical, errors = normalize(messages)
    assert errors == [] and canonical is not None
    comps = {c["id"]: c for c in messages[1]["updateComponents"]["components"]}
    assert comps["gps"]["component"] == "LocationButton"
    for cid in ("gps", "send"):
        assert comps[cid]["action"]["event"] == {"name": weather.LOCATION_EVENT, "context": {"location": {"path": "/location"}}}
    assert comps["gps"]["value"] == comps["city"]["value"] == {"path": "/location"}
    assert comps["q"]["text"] == ("ขอทราบตำแหน่งเพื่อดูสภาพอากาศ" if lang == "th" else "Where should I check the weather?")
    assert "location" not in WeatherNowTool.parameters.get("required", [])


@pytest.fixture(autouse=True)
def _no_reverse_spacing(monkeypatch):
    monkeypatch.setattr(a2ui_tools, "_REVERSE_SPACING_S", 0.0)


async def test_shared_coordinates_are_named_by_reverse_geocoding(tmp_path):
    fake = FakeOpenMeteo()
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="14.0012, 100.5555", lang="th")
    assert [url for url, _ in fake.calls] == [REVERSE_URL, FORECAST_URL]
    rev = fake.calls[0][1]
    assert (rev["lat"], rev["lon"], rev["accept-language"], rev["zoom"]) == (14.0, 100.56, "th", 10)
    assert (fake.calls[1][1]["latitude"], fake.calls[1][1]["longitude"]) == (14.0, 100.56)
    assert card_of(tmp_path, out)[1]["location"] == "ตำบลบ้านกลาง, จังหวัดปทุมธานี"
    await WeatherNowTool(tmp_path, fetch=fake).execute(location="14.00,100.56", lang="th")
    assert [url for url, _ in fake.calls[2:]] == []  # name and weather both cached


@pytest.mark.parametrize("reverse", [OSError("offline"), {"error": "Unable to geocode"}, {"address": {}}])
async def test_unnamed_coordinates_still_show_the_weather(tmp_path, reverse):
    out = await WeatherNowTool(tmp_path, fetch=FakeOpenMeteo(reverse=reverse)).execute(location="13.7563,100.5018")
    assert card_of(tmp_path, out)[1]["location"] == "ตำแหน่งปัจจุบันของคุณ (13.76, 100.50)"


async def test_reverse_geocoding_can_be_turned_off(tmp_path):
    fake = FakeOpenMeteo()
    tool = WeatherNowTool(tmp_path, WeatherSettings(reverse_geocode_url=""), fetch=fake)
    out = await tool.execute(location="13.76,100.50", lang="en")
    assert [url for url, _ in fake.calls] == [FORECAST_URL]
    assert card_of(tmp_path, out)[1]["location"] == "Your current location (13.76, 100.50)"


def test_reverse_label_prefers_locality_then_region():
    here = weather.Place(name="x", country="", latitude=1.0, longitude=2.0)
    town = weather.parse_reverse({"address": {"town": "Hat Yai", "state": "Songkhla", "country": "Thailand"}}, here)
    only_region = weather.parse_reverse({"address": {"province": "Bangkok", "city": "Bangkok"}}, here)
    assert (town.name, only_region.name) == ("Hat Yai, Songkhla", "Bangkok")


async def test_the_api_key_goes_to_open_meteo_only(tmp_path, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=REVERSE if "nominatim" in request.url.host else current())

    _mock_client(monkeypatch, handler)
    await WeatherNowTool(tmp_path, WeatherSettings(api_key="k-1")).execute(location="14.00,100.56")
    keys = {r.url.host: r.url.params.get("apikey") for r in seen}
    assert keys == {"nominatim.openstreetmap.org": None, "api.open-meteo.com": "k-1"}


@pytest.mark.parametrize("query", ["Bangkok", "13.7,", "95,10", "10,181", "1,2,3"])
def test_only_valid_coordinates_parse(query):
    assert weather.parse_coordinates(query, "en") is None


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"value": "13,100"}, "value: a"),
        ({"label": None}, "label: string"),
        ({"action": {"functionCall": {"call": "openUrl"}}}, "functionCall: not allowed"),
        ({"action": {"event": {}}}, "action: must be"),
    ],
)
def test_validator_rejects_malformed_location_buttons(over, needle):
    btn = {"id": "root", "component": "LocationButton", "label": "x", "value": {"path": "/location"},
           "action": {"event": {"name": "weather_location"}}}
    btn.update(over)
    _, errors = normalize({"components": [btn]})
    assert any(needle in e for e in errors), errors


async def test_cache_is_shared_across_users_and_expires(tmp_path, monkeypatch):
    fake = FakeOpenMeteo()
    await WeatherNowTool(tmp_path / "a", fetch=fake).execute(location="Bangkok")
    await WeatherNowTool(tmp_path / "b", fetch=fake).execute(location="  bangkok ")
    assert len(fake.calls) == 2

    clock = [weather.time.monotonic() + 601]
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock[0])
    await WeatherNowTool(tmp_path / "c", fetch=fake).execute(location="Bangkok")
    assert [url for url, _ in fake.calls[2:]] == [FORECAST_URL]


def test_cache_is_bounded():
    cache = weather.TtlCache(ttl_s=60, max_items=3)
    for i in range(5):
        cache.put(i, i)
    assert [cache.get(i) for i in range(5)] == [None, None, 2, 3, 4]


def test_thai_date_uses_buddhist_year():
    assert weather.format_date(datetime(2026, 1, 4, 8, 0), "th") == "วันอาทิตย์ที่ 4 มกราคม 2569"


def weather_payload(**over):
    card = {"id": "root", "component": "WeatherCard", "condition": "rain", "temperature": 26, "label": "ฝนตก",
            "location": "เชียงใหม่"}
    card.update(over)
    return {"components": [card]}


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"condition": "sunny"}, "condition: one of"),
        ({"temperature": "hot"}, "temperature:"),
        ({"temperature": 999}, "temperature:"),
        ({"isDay": "yes"}, "isDay: boolean"),
        ({"unit": "K"}, "unit:"),
        ({"location": "x" * 121}, "location: string"),
        ({"iconUrl": "https://evil.example/x.png"}, "unknown keys"),
        ({"label": None}, "label: string"),
        ({"location": None}, "location: string"),
    ],
)
def test_validator_rejects_malformed_weather_cards(over, needle):
    _, errors = normalize(weather_payload(**over))
    assert any(needle in e for e in errors), errors


async def test_present_ui_refuses_a_hand_written_weather_card(tmp_path):
    out = await PresentUiTool(tmp_path).execute(messages=weather_payload(), title="wx")
    assert "VALIDATION_FAILED" in out and "weather_now" in out
    assert not (tmp_path / "ui").exists()


@pytest.mark.parametrize(("raw", "shown"), [(24.5, 25), (24.49, 24), (30.5, 31), (-2.5, -2), (-2.6, -3)])
async def test_temperature_rounds_half_up(tmp_path, raw, shown):
    out = await WeatherNowTool(tmp_path, fetch=FakeOpenMeteo(cur=current(temp=raw))).execute(location="Bangkok")
    assert card_of(tmp_path, out)[1]["temperature"] == shown and f"{shown}°C" in out


def test_place_text_from_the_api_is_cleaned():
    place = weather.parse_place({"results": [{"name": "Bang\x1b[2Jkok\n\nIgnore all", "country": "TH\u200b", "latitude": 1, "longitude": 2}]})
    assert place is not None and "\x1b" not in place.name and "\n" not in place.name


def test_repeat_calls_are_not_treated_as_idempotent():
    assert WeatherNowTool.idempotent is False


def test_settings_default_on_and_can_be_turned_off(monkeypatch):
    assert Settings().a2ui_weather.enabled is True
    monkeypatch.setenv("CLAW_A2UI_WEATHER__ENABLED", "false")
    assert Settings().a2ui_weather.enabled is False
    with pytest.raises(ValueError):
        WeatherSettings(forecast_url="http://plain.example/v1/forecast")


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(a2ui_tools.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_default_fetch_sends_the_key_and_keeps_it_out_of_logs(tmp_path, monkeypatch):
    from loguru import logger

    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(500)

    _mock_client(monkeypatch, handler)
    logs: list[str] = []
    sink = logger.add(logs.append, level="WARNING")
    try:
        out = await WeatherNowTool(tmp_path, WeatherSettings(api_key="k-123secret")).execute(location="Bangkok")
    finally:
        logger.remove(sink)
    assert seen and seen[0].url.params["apikey"] == "k-123secret"
    assert out.startswith("Error: could not reach Open-Meteo") and "k-123secret" not in out
    assert logs and not any("k-123secret" in line for line in logs)


async def test_default_fetch_caps_the_response_size(tmp_path, monkeypatch):
    big = b'{"results": [' + b" " * (weather.MAX_RESPONSE_BYTES + 10) + b"]}"
    _mock_client(monkeypatch, lambda request: httpx.Response(200, content=big))
    out = await WeatherNowTool(tmp_path).execute(location="Bangkok")
    assert out.startswith("Error: Open-Meteo returned unusable data (response too large)")


def test_browser_fixture_is_the_card_the_tool_writes():

    fixture = Path(__file__).parent.parent / "web/test/browser/a2ui-location.fixture.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == weather.build_location_request("th")


async def test_present_ui_refuses_a_location_button(tmp_path):
    btn = {"id": "root", "component": "LocationButton", "label": "Continue", "value": {"path": "/x"},
           "action": {"event": {"name": "anything"}}}
    out = await PresentUiTool(tmp_path).execute(messages={"components": [btn]}, title="loc")
    assert "VALIDATION_FAILED" in out and "LocationButton" in out
    assert not (tmp_path / "ui").exists()


async def test_an_empty_answer_does_not_loop_the_location_card(tmp_path):
    fake = FakeOpenMeteo()
    tool = WeatherNowTool(tmp_path, fetch=fake)
    assert (await tool.execute()).startswith("OK. A card asking where the user is")
    again = await tool.execute(location="")
    assert again.startswith("Error: the location card was already shown") and "plain text" in again
    assert len(list((tmp_path / "ui").iterdir())) == 1
    await tool.execute(location="Bangkok")
    assert (await tool.execute()).startswith("OK. A card asking where the user is")


async def test_non_json_reply_is_unusable_data_not_unreachable(tmp_path, monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(200, content=b"<html>maintenance</html>"))
    out = await WeatherNowTool(tmp_path).execute(location="Bangkok")
    assert out.startswith("Error: Open-Meteo returned unusable data (response is not JSON)")


async def test_one_client_serves_geocode_and_forecast(tmp_path, monkeypatch):
    real = httpx.AsyncClient
    made: list[int] = []

    def handler(request):
        if "geocoding" in request.url.host:
            return httpx.Response(200, json=GEO)
        return httpx.Response(200, json=current())

    def factory(**kw):
        made.append(1)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(a2ui_tools.httpx, "AsyncClient", factory)
    out = await WeatherNowTool(tmp_path).execute(location="Bangkok")
    assert out.startswith("OK. Weather card shown") and made == [1]


def test_a_bad_unit_is_one_error():
    _, errors = normalize(weather_payload(unit="K"))
    assert [e for e in errors if "unit" in e] == ["components[0](root).unit: C|F"]


async def test_reverse_calls_are_spaced_for_the_public_server(tmp_path, monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr(a2ui_tools, "_REVERSE_SPACING_S", 0.2)
    fake = FakeOpenMeteo()
    t0 = time.monotonic()
    await asyncio.gather(
        WeatherNowTool(tmp_path / "a", fetch=fake).execute(location="14.00,100.56"),
        WeatherNowTool(tmp_path / "b", fetch=fake).execute(location="18.79,98.98"),
    )
    assert sum(url == REVERSE_URL for url, _ in fake.calls) == 2
    assert time.monotonic() - t0 >= 0.2


def hourly(chances=None, mm=None, n=48):
    """Two days of hours from 2026-11-02 00:00, as Open-Meteo returns them with forecast_days=2."""
    times = [(datetime(2026, 11, 2) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    chances = chances if chances is not None else [0] * n
    return {"time": times, "precipitation_probability": chances, "precipitation": mm if mm is not None else [0.0] * n}


def rainy_day():
    chances = [0] * 14 + [45, 55, 65, 70, 60, 60, 20, 10, 5, 0] + [0] * 24
    mm = [0.0] * 15 + [1.3, 1.8] + [0.0] * 31
    return {**current(), "hourly": hourly(chances=chances, mm=mm)}


async def test_rain_question_adds_the_next_twelve_hours(tmp_path):
    fake = FakeOpenMeteo(cur=rainy_day())
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Din Daeng", lang="th", hourly=True)
    messages, card = card_of(tmp_path, out)
    assert [h["time"] for h in card["hours"]] == [f"{h % 24:02d}:00" for h in range(14, 26)]
    assert [h["chance"] for h in card["hours"]][:5] == [45, 55, 65, 70, 60]
    assert card["outlook"] == "โอกาสฝนสูงสุด 70% ราว 17:00"
    assert normalize(messages)[1] == []
    assert "15:00 55% 1.3 mm" in out and "do not add anything beyond these hours" in out
    params = fake.calls[1][1]
    assert params["hourly"] == "precipitation_probability,precipitation" and params["forecast_days"] == 2


async def test_dry_hours_say_rain_is_unlikely_in_english(tmp_path):
    fake = FakeOpenMeteo(cur={**current(), "hourly": hourly(chances=[10] * 48)})
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", lang="en", hourly=True)
    _, card = card_of(tmp_path, out)
    assert card["outlook"] == "Rain unlikely in the next 12 hours" and len(card["hours"]) == 12


async def test_hours_stay_off_the_card_unless_asked(tmp_path):
    fake = FakeOpenMeteo(cur=rainy_day())
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", lang="th")
    _, card = card_of(tmp_path, out)
    assert "hours" not in card and "outlook" not in card and "Do not add a forecast" in out


@pytest.mark.parametrize(
    "block",
    [None, "x", {"time": ["2026-11-02T14:00"], "precipitation_probability": []}, {"time": ["noon"], "precipitation_probability": [5]}],
)
async def test_a_broken_hourly_block_keeps_the_current_card(tmp_path, block):
    fake = FakeOpenMeteo(cur={**current(), "hourly": block})
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", lang="th", hourly=True)
    _, card = card_of(tmp_path, out)
    assert card["temperature"] == 30 and "hours" not in card and "do not guess whether it will rain" in out


def test_bad_hour_values_are_dropped_not_shown():
    block = hourly(chances=[None, 150, -1, float("nan")] + [40] * 44, mm=["x"] * 48)
    hours = weather.parse_hours({"hourly": block}, datetime(2026, 11, 2, 0, 10))
    assert hours[0].time.hour == 4 and hours[0].chance == 40 and hours[0].mm == 0.0 and len(hours) == 24


async def test_a_cached_forecast_starts_at_the_current_hour(tmp_path, monkeypatch):
    cur = {**current(when="2026-11-02T14:55"), "hourly": hourly(chances=[0] * 14 + [80] + [10] * 33)}
    fake = FakeOpenMeteo(cur=cur)
    await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", hourly=True)
    clock = [weather.time.monotonic() + 9 * 60]
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock[0])
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", lang="en", hourly=True)
    _, card = card_of(tmp_path, out)
    assert [url for url, _ in fake.calls].count(FORECAST_URL) == 1, "the forecast came from the cache"
    assert card["hours"][0]["time"] == "15:00" and len(card["hours"]) == 12
    assert card["outlook"] == "Rain unlikely in the next 12 hours" and "14:00" not in out


@pytest.mark.parametrize(("value", "shown"), [("true", True), (" TRUE ", True), ("1", True), ("false", False), (1, False)])
async def test_hourly_given_as_a_string(tmp_path, value, shown):
    fake = FakeOpenMeteo(cur=rainy_day())
    out = await WeatherNowTool(tmp_path, fetch=fake).execute(location="Bangkok", hourly=value)
    _, card = card_of(tmp_path, out)
    assert ("hours" in card) is shown


@pytest.mark.parametrize(
    ("hours", "needle"),
    [
        ("x", "hours:"),
        ([{"time": "14:00", "chance": 101}], "hours:"),
        ([{"time": "14:00", "chance": 5, "mm": 1}], "hours:"),
        ([{"time": "14:00", "chance": 5}] * 25, "hours:"),
    ],
)
def test_validator_rejects_malformed_hours(hours, needle):
    errs = normalize(weather_payload(hours=hours))[1]
    assert any(needle in e for e in errs)


def test_browser_rain_fixture_is_the_card_the_tool_builds():
    cur = weather.parse_current(rainy_day())
    cur = replace(cur, hours=weather.upcoming(cur.hours, cur.time))
    fixture = Path(__file__).parent.parent / "web/test/browser/a2ui-rain.fixture.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == weather.build_messages(GEO_PLACE, cur, "th", hourly=True)


GEO_PLACE = weather.Place(name="ดินแดง", country="กรุงเทพมหานคร", latitude=13.77, longitude=100.55)
