import copy
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from the_bot_radio.services.inputs.base import TZ, RunContext, Settings, make_http_client
from the_bot_radio.services.inputs.citibike import (
    INFO_URL,
    STATUS_URL,
    CitiBikeService,
    in_zip,
    zip_polygons,
)

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

# Fixtures: GBFS recorded 2026-10-26 ~14:40 ET, trimmed to the 40 stations inside 11222
# (38 renting, 2 not installed) plus 3 outside it.
REPORTED = datetime.fromtimestamp(1793039998, TZ)  # newest last_reported in the fixture
NOW = REPORTED + timedelta(minutes=5)


def fixtures() -> tuple[dict, dict]:
    return load_fixture("citibike", "station_status.json"), load_fixture(
        "citibike", "station_information.json"
    )


def mock_feeds(status=None, info=None) -> tuple[respx.Route, respx.Route]:
    s_fix, i_fix = fixtures()
    s = respx.get(STATUS_URL).mock(return_value=status or httpx.Response(200, json=s_fix))
    i = respx.get(INFO_URL).mock(return_value=info or httpx.Response(200, json=i_fix))
    return s, i


@respx.mock
async def test_sums_every_renting_station_in_the_zip(make_ctx):
    status_route, info_route = mock_feeds()
    reading = await CitiBikeService().fetch(make_ctx(now=NOW))

    assert status_route.called and info_route.called
    assert reading.key == "citibike" and reading.label == "Citi Bikes · Greenpoint"
    assert reading.status == "ok"
    assert reading.value == "537 of 1283 docked"
    data = reading.data
    assert (data["bikes"], data["ebikes"], data["docks"], data["capacity"]) == (537, 177, 670, 1283)
    assert data["zip"] == "11222"
    assert data["station_count"] == 38
    assert sorted(data["offline_stations"]) == ["Clay St & Commercial St", "West St & Freeman St"]
    assert data["stale_stations"] == []
    assert data["last_reported"] == REPORTED.isoformat()
    names = {s["name"] for s in data["stations"]}
    assert "Leonard St & Nassau Ave" in names
    assert names.isdisjoint({"W 21 St & 6 Ave", "N 6 St & Bedford Ave", "Broadway & E 14 St"})
    assert len(reading.raw["station_ids"]) == 40


@respx.mock
async def test_stale_stations_are_left_out_of_the_sum(make_ctx):
    status, _ = fixtures()
    leonard = next(s for s in status["data"]["stations"] if s["station_id"].startswith("7a771842"))
    leonard["last_reported"] -= 2 * 3600
    mock_feeds(status=httpx.Response(200, json=status))

    reading = await CitiBikeService().fetch(make_ctx(now=NOW))
    assert reading.status == "ok"
    assert reading.data["station_count"] == 37
    assert reading.data["stale_stations"] == ["Leonard St & Nassau Ave"]
    assert reading.data["bikes"] == 537 - leonard["num_bikes_available"]


@respx.mock
async def test_all_stale_falls_back_to_stale_totals(make_ctx):
    mock_feeds()
    reading = await CitiBikeService().fetch(make_ctx(now=REPORTED + timedelta(hours=2)))
    assert reading.status == "stale"
    assert reading.value == "537 of 1283 docked"


@respx.mock
async def test_no_stations_in_zip_raises(make_ctx):
    _, info = fixtures()
    outside = {"W 21 St & 6 Ave", "N 6 St & Bedford Ave", "Broadway & E 14 St"}
    info["data"]["stations"] = [s for s in info["data"]["stations"] if s["name"] in outside]
    mock_feeds(info=httpx.Response(200, json=info))
    with pytest.raises(ValueError, match="no Citi Bike stations found in ZIP 11222"):
        await CitiBikeService().fetch(make_ctx(now=NOW))


async def test_unknown_zip_raises(make_ctx):
    with pytest.raises(ValueError, match="no boundary for ZIP 10001"):
        await CitiBikeService().fetch(make_ctx(settings=Settings(zip_code="10001")))


@respx.mock
async def test_http_error_raises(make_ctx):
    mock_feeds(status=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await CitiBikeService().fetch(make_ctx(now=NOW))


@respx.mock
async def test_malformed_payload_raises(make_ctx):
    mock_feeds(info=httpx.Response(200, json={"data": {}}))
    with pytest.raises(ValueError, match="malformed station_information"):
        await CitiBikeService().fetch(make_ctx(now=NOW))


@respx.mock
async def test_malformed_station_record_raises(make_ctx):
    status, _ = fixtures()
    broken = copy.deepcopy(status)
    for s in broken["data"]["stations"]:
        s.pop("num_docks_available")
    mock_feeds(status=httpx.Response(200, json=broken))
    with pytest.raises(ValueError, match="malformed station record"):
        await CitiBikeService().fetch(make_ctx(now=NOW))


def test_in_zip():
    polygons = zip_polygons("11222")
    assert in_zip(-73.9512, 40.7244, polygons)  # Nassau Av G stop
    assert in_zip(-73.9541, 40.7295, polygons)  # Greenpoint center
    assert not in_zip(-73.9573, 40.7173, polygons)  # N 6 St & Bedford Ave, Williamsburg
    assert not in_zip(-73.9903, 40.7359, polygons)  # Union Square


@pytest.mark.live
async def test_live():
    now = datetime.now(TZ)
    async with make_http_client() as http:
        reading = await CitiBikeService().fetch(RunContext(run_date=now.date(), now=now, http=http))
    print(f"\n{reading.status}: {reading.value!r} ({reading.data['station_count']} stations)")
    assert reading.data["station_count"] > 20
    assert reading.data["capacity"] > 0
