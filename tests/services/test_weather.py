import copy
from datetime import datetime

import httpx
import pytest
import respx

from the_bot_radio.services.inputs import weather
from the_bot_radio.services.inputs.base import TZ
from the_bot_radio.services.inputs.weather import WeatherService, parse_wind_mph, shorten_condition

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

POINTS_URL = "https://api.weather.gov/points/40.7295,-73.954"
HOURLY_URL = "https://api.weather.gov/gridpoints/OKX/35,43/forecast/hourly"
DAILY_URL = "https://api.weather.gov/gridpoints/OKX/35,43/forecast"
# Fixtures were recorded on 2026-10-26 at ~14:30 ET; first hourly period starts 14:00.
NOW = datetime(2026, 10, 26, 14, 30, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(weather, "RETRY_DELAY", 0)


def _mock(router, hourly=None, daily=None):
    router.get(POINTS_URL).respond(json=load_fixture("weather", "points.json"))
    hourly_route = router.get(HOURLY_URL).respond(
        json=hourly or load_fixture("weather", "forecast_hourly.json")
    )
    router.get(DAILY_URL).respond(json=daily or load_fixture("weather", "forecast_daily.json"))
    return hourly_route


async def test_happy_path(make_ctx):
    with respx.mock(assert_all_called=True) as router:
        _mock(router)
        r = await WeatherService().fetch(make_ctx(now=NOW))
        assert router.calls.call_count == 3
        req = router.calls[0].request
    assert req.headers["Accept"] == "application/geo+json"
    assert "TheBotRadio" in req.headers["User-Agent"]
    assert r.key == "weather" and r.label == "Weather · Greenpoint" and r.status == "ok"
    assert r.value == "68°F, sunny"
    assert r.data == {
        "temp_f": 68,
        "conditions": "sunny",
        "short_forecast": "Sunny",
        "precip_chance": 1,  # max over 14:00..01:00
        "wind_mph": 7,
        "wind_dir": "W",
        "high_f": 70,
        "low_f": 52,
        "forecast_period_start": "2026-10-26T14:00:00-04:00",
        "forecast_updated": "2026-10-26T06:57:31+00:00",
    }
    assert len(r.raw["hourly"]) == 12 and r.raw["grid"]["gridId"] == "OKX"
    assert len(r.raw["daily"]) == 2


async def test_picks_period_covering_now_and_upper_wind(make_ctx):
    hourly = load_fixture("weather", "forecast_hourly.json")
    hourly["properties"]["periods"][2].update(
        windSpeed="10 to 15 mph", shortForecast="Chance Light Rain"
    )
    hourly["properties"]["periods"][5]["probabilityOfPrecipitation"]["value"] = 40
    with respx.mock() as router:
        _mock(router, hourly=hourly)
        r = await WeatherService().fetch(make_ctx(now=datetime(2026, 10, 26, 16, 10, tzinfo=TZ)))
    assert r.status == "ok"  # first period is 2h10m old: within the 3h window
    assert r.value == "70°F, chance of light rain"
    assert r.data["wind_mph"] == 15 and r.data["precip_chance"] == 40
    assert r.data["forecast_period_start"] == "2026-10-26T16:00:00-04:00"


async def test_stale_when_first_period_older_than_3h(make_ctx):
    with respx.mock() as router:
        _mock(router)
        r = await WeatherService().fetch(make_ctx(now=datetime(2026, 10, 26, 17, 1, tzinfo=TZ)))
    assert r.status == "stale"
    assert r.value  # data is still present


async def test_5xx_once_then_success(make_ctx):
    with respx.mock() as router:
        _mock(router)
        hourly_route = router.get(HOURLY_URL)
        hourly_route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, json=load_fixture("weather", "forecast_hourly.json")),
        ]
        r = await WeatherService().fetch(make_ctx(now=NOW))
        assert hourly_route.call_count == 2
    assert r.status == "ok" and r.value == "68°F, sunny"


async def test_5xx_twice_raises(make_ctx):
    with respx.mock() as router:
        _mock(router)
        hourly_route = router.get(HOURLY_URL).respond(500)
        with pytest.raises(httpx.HTTPStatusError):
            await WeatherService().fetch(make_ctx(now=NOW))
        assert hourly_route.call_count == 2


async def test_4xx_does_not_retry(make_ctx):
    with respx.mock() as router:
        points_route = router.get(POINTS_URL).respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            await WeatherService().fetch(make_ctx(now=NOW))
        assert points_route.call_count == 1


async def test_points_missing_urls_raises(make_ctx):
    with respx.mock() as router:
        router.get(POINTS_URL).respond(json={"properties": {}})
        with pytest.raises(ValueError):
            await WeatherService().fetch(make_ctx(now=NOW))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h["properties"].update(periods=[]),
        lambda h: h.pop("properties"),
        lambda h: h["properties"]["periods"][0].pop("temperature"),
        lambda h: h["properties"]["periods"][0].update(startTime="not a date"),
    ],
)
async def test_malformed_hourly_raises(make_ctx, mutate):
    hourly = copy.deepcopy(load_fixture("weather", "forecast_hourly.json"))
    mutate(hourly)
    with respx.mock() as router:
        _mock(router, hourly=hourly)
        with pytest.raises(ValueError):
            await WeatherService().fetch(make_ctx(now=NOW))


async def test_high_low_missing_after_dark(make_ctx):
    daily = load_fixture("weather", "forecast_daily.json")
    daily["properties"]["periods"] = daily["properties"]["periods"][2:]  # starts tomorrow
    with respx.mock() as router:
        _mock(router, daily=daily)
        r = await WeatherService().fetch(make_ctx(now=NOW))
    assert r.data["high_f"] is None and r.data["low_f"] is None


@pytest.mark.parametrize(
    ("short", "expected"),
    [
        ("Sunny", "sunny"),
        ("Mostly Cloudy", "mostly cloudy"),
        ("Chance Light Rain", "chance of light rain"),
        ("Slight Chance Rain Showers", "chance of showers"),
        ("Rain Showers Likely", "showers likely"),
        ("Chance Showers And Thunderstorms", "chance of t-storms"),
        ("Showers And Thunderstorms Likely", "t-storms likely"),
        ("Areas Of Fog", "fog"),
        ("Patchy Fog then Mostly Sunny", "fog"),
        ("Isolated Rain Showers", "showers"),
        ("Light Snow Likely", "light snow likely"),
        ("Chance Rain And Snow Showers", "rain and snow"),
        ("Slight Chance Light Freezing Drizzle", "light freezing drizzle"),
    ],
)
def test_shorten_condition(short, expected):
    assert shorten_condition(short) == expected
    assert len(shorten_condition(short)) <= weather.MAX_CONDITION_LEN


@pytest.mark.parametrize(
    ("speed", "mph"), [("7 mph", 7), ("10 to 15 mph", 15), ("0 mph", 0), ("", 0)]
)
def test_parse_wind_mph(speed, mph):
    assert parse_wind_mph(speed) == mph


@pytest.mark.live
async def test_live(make_ctx):
    r = await WeatherService().fetch(make_ctx(now=datetime.now(TZ)))
    print(f"\nlive weather: {r.value!r} status={r.status} data={r.data}")
    assert r.status == "ok"
    assert r.value.endswith(r.data["conditions"]) and "°F, " in r.value
    assert 0 <= r.data["precip_chance"] <= 100
