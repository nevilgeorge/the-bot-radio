import copy
from datetime import datetime

import httpx
import pytest
import respx

from the_bot_radio.services.inputs.base import TZ
from the_bot_radio.services.inputs.g_train import ALERTS_URL, GTrainService, category

from .conftest import load_fixture

pytestmark = pytest.mark.anyio

LIVE_FEED = "alerts_2026-10-26.json"  # recorded 2026-10-26 14:28 ET, real G "Delays" alert
AT_1430 = datetime(2026, 10, 26, 14, 30, tzinfo=TZ)
AT_2200 = datetime(2026, 10, 26, 22, 0, tzinfo=TZ)


def feed(name: str = LIVE_FEED) -> dict:
    return copy.deepcopy(load_fixture("g_train", name))


def is_g(entity: dict) -> bool:
    return any(e.get("route_id") == "G" for e in entity["alert"]["informed_entity"])


def alert_type(entity: dict) -> str:
    return entity["alert"]["transit_realtime.mercury_alert"]["alert_type"]


async def fetch(make_ctx, payload: dict, now: datetime):
    with respx.mock:
        route = respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=payload))
        reading = await GTrainService().fetch(make_ctx(now=now))
    assert route.called
    assert "x-api-key" not in route.calls.last.request.headers
    return reading


async def test_delays_from_recorded_feed(make_ctx):
    r = await fetch(make_ctx, feed(), AT_1430)
    assert r.key == "g_train" and r.label == "G train" and r.status == "ok"
    assert r.value == "Delays, both directions"
    assert r.data["status"] == "delays"
    assert (r.data["delayed"], r.data["suspended"], r.data["planned"]) == (True, False, False)
    [alert] = r.data["alerts"]  # the F/M delay and future G planned work are ignored
    assert alert["type"] == "Delays" and alert["direction"] == "both"
    assert alert["active_from"] == "2026-10-26T14:13:25-04:00" and alert["active_until"] is None
    assert [e["id"] for e in r.raw["entity"]] == ["lmm:alert:273548:26"]


async def test_delays_northbound_from_direction_id(make_ctx):
    payload = feed()
    for e in payload["entity"]:
        if is_g(e) and alert_type(e) == "Delays":
            e["alert"]["header_text"]["translation"][0]["text"] = (
                "Court Sq-bound [G] trains are running with delays while we fix a switch."
            )
            e["alert"]["informed_entity"][0]["direction_id"] = 0
    r = await fetch(make_ctx, payload, AT_1430)
    assert r.value == "Delays northbound"
    assert r.data["alerts"][0]["direction"] == "northbound"


async def test_good_service_when_no_g_alerts(make_ctx):
    payload = feed()
    payload["entity"] = [e for e in payload["entity"] if not is_g(e)]
    assert payload["entity"]  # other lines still have alerts
    r = await fetch(make_ctx, payload, AT_1430)
    assert r.value == "Good service"
    assert r.data["status"] == "good" and r.data["alerts"] == [] and r.raw["entity"] == []
    assert not (r.data["delayed"] or r.data["suspended"] or r.data["planned"])


async def test_suspension_with_segment(make_ctx):
    now = datetime(2026, 10, 26, 10, 10, tzinfo=TZ)
    r = await fetch(make_ctx, feed("alerts_suspended.json"), now)
    assert r.value == "Suspended, Court Sq–Hoyts"
    assert r.data["status"] == "suspended" and r.data["suspended"] and not r.data["delayed"]


async def test_planned_only_overnight(make_ctx):
    payload = feed()
    payload["entity"] = [e for e in payload["entity"] if alert_type(e) != "Delays"]
    payload["header"]["timestamp"] = int(AT_2200.timestamp()) - 60
    r = await fetch(make_ctx, payload, AT_2200)
    assert r.value == "Planned work, overnight"
    assert r.data["status"] == "planned" and r.data["planned"] and not r.data["delayed"]
    [alert] = r.data["alerts"]
    assert alert["type"] == "Planned - Stops Skipped" and alert["direction"] == "both"
    assert alert["active_until"] == "2026-10-27T05:00:00-04:00"


async def test_expired_alert_ignored(make_ctx):
    payload = feed()
    for e in payload["entity"]:
        if is_g(e) and alert_type(e) == "Delays":
            e["alert"]["active_period"] = [{"start": 1793030000, "end": 1793035000}]
    r = await fetch(make_ctx, payload, AT_1430)
    assert r.value == "Good service" and r.data["alerts"] == []


async def test_station_notice_keeps_good_service(make_ctx):
    payload = feed()
    for e in payload["entity"]:
        if is_g(e) and alert_type(e) == "Delays":
            e["alert"]["transit_realtime.mercury_alert"]["alert_type"] = "Station Notice"
    r = await fetch(make_ctx, payload, AT_1430)
    assert r.value == "Good service" and r.data["status"] == "good"
    assert r.data["alerts"][0]["type"] == "Station Notice"


async def test_old_feed_is_stale(make_ctx):
    r = await fetch(make_ctx, feed(), datetime(2026, 10, 27, 15, 0, tzinfo=TZ))
    assert r.status == "stale" and r.value == "Delays, both directions"


@pytest.mark.parametrize("payload", [{}, {"header": {}, "entity": "nope"}, [], {"entity": []}])
async def test_malformed_feed_raises(make_ctx, payload):
    with respx.mock:
        respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=payload))
        with pytest.raises(ValueError):
            await GTrainService().fetch(make_ctx(now=AT_1430))


async def test_http_error_raises(make_ctx):
    with respx.mock:
        respx.get(ALERTS_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            await GTrainService().fetch(make_ctx(now=AT_1430))


def test_category_mapping():
    assert category("Delays") == category("Some Delays") == "delays"
    assert category("Part Suspended") == "suspended"
    assert category("Planned - Part Suspended") == "planned"
    assert category("Reduced Service") == category("Reroute") == "other"
    assert category("Station Notice") == "info"


@pytest.mark.live
async def test_live(make_ctx):
    r = await GTrainService().fetch(make_ctx(now=datetime.now(TZ)))
    print(f"\nG train live: {r.value!r} status={r.status} data={r.data}")
    assert r.status == "ok" and r.value
