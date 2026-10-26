from datetime import date, datetime

import httpx
import pytest
import respx

from the_bot_radio.services.inputs.base import TZ, RunContext, Settings, make_http_client
from the_bot_radio.services.inputs.tide import STATION, URL, TideService

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 26)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=TZ)


@respx.mock
async def test_happy_path(make_ctx):
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("tide", "predictions_8517673_20261026.json")
        )
    )
    reading = await TideService().fetch(make_ctx(run_date=DAY, now=_at(6)))

    params = route.calls.last.request.url.params
    assert params["station"] == STATION == "8517673"
    assert params["begin_date"] == params["end_date"] == "20261026"
    assert params["interval"] == "hilo" and params["datum"] == "MLLW"
    assert params["time_zone"] == "lst_ldt" and params["product"] == "predictions"

    assert reading.status == "ok"
    assert reading.key == "tide" and reading.label == "Newtown Creek tide"
    assert reading.value == "High at 9:49 AM"
    data = reading.data
    assert data["station_id"] == "8517673"
    assert data["station_name"] == "Hunters Point, Newtown Creek"
    assert [e["type"] for e in data["events"]] == ["L", "H", "L", "H"]
    assert data["events"][1] == {
        "type": "H",
        "time": "2026-10-26T09:49:00-04:00",
        "height_ft": 5.324,
    }
    assert data["next_high"] == "2026-10-26T09:49:00-04:00"
    assert data["next_low"] == "2026-10-26T16:15:00-04:00"
    assert len(reading.raw["predictions"]) == 4


@pytest.mark.parametrize(
    ("now", "value", "next_high", "next_low"),
    [
        (_at(9, 49), "High at 9:49 AM", "2026-10-26T09:49:00-04:00", "2026-10-26T16:15:00-04:00"),
        (_at(12), "High at 10:18 PM", "2026-10-26T22:18:00-04:00", "2026-10-26T16:15:00-04:00"),
        (_at(17), "High at 10:18 PM", "2026-10-26T22:18:00-04:00", None),
        (_at(23), "High was 10:18 PM", None, None),
    ],
)
@respx.mock
async def test_next_high_selection(make_ctx, now, value, next_high, next_low):
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("tide", "predictions_8517673_20261026.json")
        )
    )
    reading = await TideService().fetch(make_ctx(run_date=DAY, now=now))
    assert reading.value == value
    assert reading.data["next_high"] == next_high
    assert reading.data["next_low"] == next_low


@respx.mock
async def test_error_in_200_raises(make_ctx):
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=load_fixture("tide", "error_200.json"))
    )
    with pytest.raises(ValueError, match="No Predictions data"):
        await TideService().fetch(make_ctx())


@pytest.mark.parametrize("payload", [{"predictions": []}, {}])
@respx.mock
async def test_empty_predictions_raise(make_ctx, payload):
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="no tide predictions"):
        await TideService().fetch(make_ctx())


@respx.mock
async def test_http_error_raises(make_ctx):
    respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await TideService().fetch(make_ctx())


@respx.mock
async def test_settings_override_station(make_ctx):
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("tide", "predictions_8517673_20261026.json")
        )
    )
    reading = await TideService().fetch(make_ctx(settings=Settings(noaa_tide_station="8518750")))
    assert route.calls.last.request.url.params["station"] == "8518750"
    assert reading.data["station_id"] == "8518750"
    assert reading.data["station_name"] == "The Battery"


@pytest.mark.live
async def test_live():
    now = datetime.now(TZ)
    async with make_http_client() as http:
        reading = await TideService().fetch(RunContext(run_date=now.date(), now=now, http=http))
    print(f"\n{reading.value!r}\n{reading.data}")
    assert reading.value.startswith("High ")
    assert reading.data["events"]
