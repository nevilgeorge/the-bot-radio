from datetime import date, datetime, timedelta

import httpx
import pytest
import respx

from the_bot_radio.services.inputs.base import TZ, Settings
from the_bot_radio.services.inputs.sunset import SunsetService, format_clock

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

REFERENCE_URL = "https://api.sunrise-sunset.org/json"


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _assert_close(ours: str, ref: str, minutes: int = 2) -> None:
    diff = abs(datetime.fromisoformat(ours) - _utc(ref))
    assert diff <= timedelta(minutes=minutes), (ours, ref, diff)


async def test_happy_path_no_http(make_ctx):
    # The site fixture (tests/fixtures/days/2026-10-26.json) says 5:52 PM, but that is Oct 31's
    # sunset; the true 2026-10-26 Greenpoint sunset is ~5:59 PM EDT (astral and
    # sunrise-sunset.org agree within a minute).
    with respx.mock() as router:  # no routes: any HTTP call would fail the test
        r = await SunsetService().fetch(make_ctx())
    assert router.calls.call_count == 0
    assert r.key == "sunset" and r.label == "Sunset" and r.status == "ok"
    assert r.value == "5:59 PM"
    assert r.data["sunset"].startswith("2026-10-26T17:59") and r.data["sunset"].endswith("-04:00")
    assert r.data["sunrise"].startswith("2026-10-26T07:19")
    assert r.data["civil_dusk"].startswith("2026-10-26T18:2")
    assert r.data["golden_hour_start"] < r.data["sunset"]
    assert 635 <= r.data["day_length_min"] <= 645
    assert r.raw == {"lat": 40.7295, "lon": -73.954, "date": "2026-10-26"}


async def test_matches_recorded_reference(make_ctx):
    ref = load_fixture("sunset", "sunrise_sunset_org_2026-10-26.json")["results"]
    r = await SunsetService().fetch(make_ctx())
    _assert_close(r.data["sunset"], ref["sunset"])
    _assert_close(r.data["sunrise"], ref["sunrise"])
    _assert_close(r.data["civil_dusk"], ref["civil_twilight_end"])
    assert abs(r.data["day_length_min"] - ref["day_length"] / 60) <= 2


async def test_dst_boundary(make_ctx):
    before = await SunsetService().fetch(make_ctx(date(2026, 10, 31)))
    after = await SunsetService().fetch(make_ctx(date(2026, 11, 1)))
    assert before.value == "5:53 PM" and before.data["sunset"].endswith("-04:00")
    assert after.value == "4:52 PM" and after.data["sunset"].endswith("-05:00")
    b, a = (
        datetime.fromisoformat(before.data["sunset"]),
        datetime.fromisoformat(after.data["sunset"]),
    )
    # Wall clock jumps ~1h earlier...
    clock_jump = b.replace(tzinfo=None) - (a.replace(tzinfo=None) - timedelta(days=1))
    assert timedelta(minutes=58) <= clock_jump <= timedelta(minutes=63)
    # ...while the real instant only moves ~1 minute.
    assert abs(a - b - timedelta(days=1)) < timedelta(minutes=3)


async def test_summer_solstice(make_ctx):
    r = await SunsetService().fetch(make_ctx(date(2026, 6, 21)))
    assert r.value in {"8:30 PM", "8:31 PM"}
    assert r.data["day_length_min"] > 900


def test_format_clock_no_leading_zero_and_rounding():
    assert format_clock(datetime(2026, 11, 1, 16, 51, 42, tzinfo=TZ)) == "4:52 PM"
    assert format_clock(datetime(2026, 6, 21, 9, 5, 10, tzinfo=TZ)) == "9:05 AM"
    assert format_clock(datetime(2026, 6, 21, 0, 0, 0, tzinfo=TZ)) == "12:00 AM"
    assert format_clock(datetime(2026, 6, 21, 12, 59, 31, tzinfo=TZ)) == "1:00 PM"


async def test_polar_location_raises(make_ctx):
    # Svalbard in December: the sun never rises, astral raises → runner marks "skipped".
    ctx = make_ctx(date(2026, 12, 21), settings=Settings(lat=78.22, lon=15.65))
    with pytest.raises(ValueError):
        await SunsetService().fetch(ctx)


@pytest.mark.live
async def test_live_against_sunrise_sunset_org(make_ctx):
    ctx = make_ctx(datetime.now(TZ).date())
    r = await SunsetService().fetch(ctx)
    params = {
        "lat": ctx.settings.lat,
        "lng": ctx.settings.lon,
        "date": ctx.run_date.isoformat(),
        "formatted": 0,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(REFERENCE_URL, params=params)
    resp.raise_for_status()
    ref = resp.json()["results"]
    print(f"\nastral: {r.value} {r.data}\nreference: {ref['sunset']}")
    _assert_close(r.data["sunset"], ref["sunset"])
    _assert_close(r.data["sunrise"], ref["sunrise"])
