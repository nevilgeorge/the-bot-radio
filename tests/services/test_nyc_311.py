from datetime import date, datetime

import httpx
import pytest
import respx

from the_bot_radio.services.inputs.base import TZ, RunContext, Settings, make_http_client
from the_bot_radio.services.inputs.nyc_311 import ENDPOINT, Nyc311Service, headline

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

KEY = "nyc_311"


def mock_api(watermark, grouped):
    """Route the watermark (max(created_date)) and grouped queries to separate payloads."""
    route = respx.get(ENDPOINT)

    def respond(request: httpx.Request) -> httpx.Response:
        select = request.url.params["$select"]
        return httpx.Response(200, json=watermark if select.startswith("max(") else grouped)

    route.side_effect = respond
    return route


def params_of(route, kind: str) -> httpx.QueryParams:
    for call in route.calls:
        params = call.request.url.params
        if params["$select"].startswith("max(") == (kind == "watermark"):
            return params
    raise AssertionError(f"no {kind} request")


def rows(day: str, counts: dict[str, int]) -> list[dict]:
    return [
        {"day": f"{day}T00:00:00.000", "complaint_type": t, "n": str(n)} for t, n in counts.items()
    ]


@respx.mock
async def test_happy_path_complete_yesterday(make_ctx):
    # Recorded 2026-10-26: watermark 2026-10-25T03:05, so a 2026-10-25 run sees 10-24 complete.
    route = mock_api(
        load_fixture(KEY, "watermark.json"), load_fixture(KEY, "grouped_2026-10-24_25.json")
    )
    reading = await Nyc311Service().fetch(make_ctx(run_date=date(2026, 10, 25)))

    assert reading.status == "ok"
    assert reading.value == "47 noise complaints"  # 20+19+7+1 Noise* > 39 Illegal Parking
    assert reading.key == "nyc_311_yesterday" and reading.label == "311, yesterday"
    d = reading.data
    assert d["date"] == "2026-10-24" and d["zip"] == "11222"
    assert d["total"] == 120 and d["noise"] == 47
    assert d["top_type"] == "Illegal Parking"
    assert len(d["by_type"]) == 10 and d["by_type"]["Illegal Parking"] == 39
    assert d["complete_through"] == "2026-10-25T03:05:51"
    assert len(reading.raw["rows"]) == 23

    grouped = params_of(route, "grouped")
    assert grouped["$where"] == (
        "incident_zip='11222' AND created_date >= '2026-10-23T00:00:00'"
        " AND created_date < '2026-10-25T00:00:00'"
    )
    assert grouped["$group"] == "day, complaint_type"
    assert params_of(route, "watermark")["$where"] == "created_date >= '2026-10-23T00:00:00'"
    assert "X-App-Token" not in route.calls[0].request.headers


@respx.mock
async def test_yesterday_incomplete_falls_back_to_day_before_as_stale(make_ctx):
    # The real 06:00 situation: yesterday (10-25) has only ~3h of rows loaded.
    mock_api(load_fixture(KEY, "watermark.json"), load_fixture(KEY, "grouped_2026-10-24_25.json"))
    reading = await Nyc311Service().fetch(make_ctx())  # run_date 2026-10-26

    assert reading.status == "stale"
    assert reading.value == "Sat: 47 noise complaints"
    assert reading.data["date"] == "2026-10-24"
    assert reading.data["requested_date"] == "2026-10-25"


@respx.mock
async def test_nothing_complete_is_stale_no_data(make_ctx):
    mock_api([{"latest": "2026-10-24T02:00:00.000"}], rows("2026-10-24", {"Rodent": 2}))
    reading = await Nyc311Service().fetch(make_ctx())
    assert reading.status == "stale"
    assert reading.value == "No data yet"
    assert reading.data["date"] is None


@respx.mock
async def test_null_watermark_is_stale_no_data(make_ctx):
    mock_api([{}], [])
    reading = await Nyc311Service().fetch(make_ctx())
    assert (reading.status, reading.value) == ("stale", "No data yet")


@respx.mock
async def test_complete_but_empty_day_is_ok_zero(make_ctx):
    mock_api([{"latest": "2026-10-26T02:10:00.000"}], [])
    reading = await Nyc311Service().fetch(make_ctx())
    assert reading.status == "ok"
    assert reading.value == "0 requests"
    assert reading.data["total"] == 0 and reading.data["noise"] == 0
    assert reading.data["top_type"] is None and reading.data["by_type"] == {}


@respx.mock
async def test_total_led_when_noise_is_not_top(make_ctx):
    grouped = rows(
        "2026-10-25", {"Illegal Parking": 54, "HEAT/HOT WATER": 10, "Noise - Commercial": 8}
    )
    mock_api([{"latest": "2026-10-26T03:00:00.000"}], grouped)
    reading = await Nyc311Service().fetch(make_ctx())
    assert reading.value == "72 requests"
    assert reading.data["noise"] == 8 and reading.data["top_type"] == "Illegal Parking"


def test_headline_rule():
    assert headline(63, 41, 12) == "41 noise complaints"
    assert headline(63, 12, 41) == "63 requests"
    assert headline(10, 5, 5) == "5 noise complaints"  # ties go to noise
    assert headline(1, 1, 0) == "1 noise complaint"
    assert headline(1, 0, 1) == "1 request"


@respx.mock
async def test_app_token_header_sent(make_ctx):
    route = mock_api([{"latest": "2026-10-26T03:00:00.000"}], [])
    await Nyc311Service().fetch(
        make_ctx(settings=Settings(nyc_open_data_app_token="tok", zip_code="11211"))
    )
    assert all(c.request.headers["X-App-Token"] == "tok" for c in route.calls)
    assert "incident_zip='11211'" in params_of(route, "grouped")["$where"]


@respx.mock
async def test_http_error_raises(make_ctx):
    respx.get(ENDPOINT).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await Nyc311Service().fetch(make_ctx())


@respx.mock
async def test_socrata_error_object_raises(make_ctx):
    error = {"code": "dataset.missing", "error": True, "message": "Not found"}
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=error))
    with pytest.raises(ValueError, match="unexpected 311 payload"):
        await Nyc311Service().fetch(make_ctx())


@respx.mock
async def test_malformed_rows_raise(make_ctx):
    mock_api([{"latest": "2026-10-26T03:00:00.000"}], [{"complaint_type": "Rodent", "n": "x"}])
    with pytest.raises(ValueError, match="malformed"):
        await Nyc311Service().fetch(make_ctx())


@pytest.mark.live
async def test_live():
    now = datetime.now(TZ)
    async with make_http_client() as http:
        reading = await Nyc311Service().fetch(RunContext(run_date=now.date(), now=now, http=http))
    print(f"\n{reading.status} {reading.value!r} {reading.data}")
    assert reading.status in ("ok", "stale")
    assert len(reading.value) <= 28
