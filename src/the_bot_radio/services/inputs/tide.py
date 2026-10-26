"""Newtown Creek tide: today's high/low predictions from NOAA CO-OPS.

Source: NOAA Tides & Currents data API (no key)
  GET https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
      ?product=predictions&interval=hilo&datum=MLLW&time_zone=lst_ldt&units=english&format=json
Default station 8517673 "Hunters Point, Newtown Creek" (subordinate to The Battery), ~1.3 km
from Greenpoint. Override with Settings.noaa_tide_station.
"""

from datetime import date, datetime
from typing import Any, ClassVar

from .base import TZ, Reading, RunContext

URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
STATION = "8517673"
STATION_NAMES = {
    "8517673": "Hunters Point, Newtown Creek",
    "8518750": "The Battery",
}

TideEvent = tuple[str, datetime, float]  # (type "H"/"L", local aware time, height ft)


def _query_params(station_id: str, day: date) -> dict[str, str]:
    """Query string for one day of high/low predictions at `station_id`."""
    yyyymmdd = day.strftime("%Y%m%d")
    return {
        "product": "predictions",
        "interval": "hilo",
        "datum": "MLLW",
        "time_zone": "lst_ldt",
        "units": "english",
        "format": "json",
        "application": "TheBotRadio",
        "station": station_id,
        "begin_date": yyyymmdd,
        "end_date": yyyymmdd,
    }


def _format_clock(moment: datetime) -> str:
    """'3:14 PM'."""
    return moment.strftime("%I:%M %p").lstrip("0")


def _parse_predictions(predictions: list[dict[str, Any]]) -> list[TideEvent]:
    """(type, local aware time, height ft), sorted by time. `t` is local wall time (lst_ldt)."""
    tide_events = [
        (
            prediction["type"],
            datetime.strptime(prediction["t"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ),
            float(prediction["v"]),
        )
        for prediction in predictions
    ]
    return sorted(tide_events, key=lambda tide_event: tide_event[1])


def _next_event_time(
    tide_events: list[TideEvent], event_type: str, now: datetime
) -> datetime | None:
    """Time of the first `event_type` ("H" or "L") at or after `now`, if any remain today."""
    return next(
        (
            event_time
            for current_type, event_time, _ in tide_events
            if current_type == event_type and event_time >= now
        ),
        None,
    )


class TideService:
    """Today's tide predictions, headlined by the next high tide."""

    key: ClassVar[str] = "tide"
    label: ClassVar[str] = "Newtown Creek tide"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Fetch today's high/low predictions and find the next high and low."""
        station_id = ctx.settings.noaa_tide_station or STATION
        response = await ctx.http.get(URL, params=_query_params(station_id, ctx.run_date))
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:  # NOAA often reports errors as HTTP 200 + {"error": {...}}
            raise ValueError(f"NOAA error: {payload['error'].get('message', '').strip()}")
        predictions = payload.get("predictions") or []
        if not predictions:
            raise ValueError(f"no tide predictions for station {station_id} on {ctx.run_date}")

        tide_events = _parse_predictions(predictions)
        high_tide_times = [
            event_time for event_type, event_time, _ in tide_events if event_type == "H"
        ]
        if not high_tide_times:
            raise ValueError(f"no high tide in predictions for station {station_id}")
        now = ctx.now.astimezone(TZ)
        next_high = _next_event_time(tide_events, "H", now)
        next_low = _next_event_time(tide_events, "L", now)
        # After the day's last high, report when it was instead.
        if next_high:
            value = f"High at {_format_clock(next_high)}"
        else:
            value = f"High was {_format_clock(high_tide_times[-1])}"

        return ctx.reading(
            self,
            value,
            data={
                "station_id": station_id,
                "station_name": STATION_NAMES.get(station_id, station_id),
                "events": [
                    {"type": event_type, "time": event_time.isoformat(), "height_ft": height_ft}
                    for event_type, event_time, height_ft in tide_events
                ],
                "next_high": next_high.isoformat() if next_high else None,
                "next_low": next_low.isoformat() if next_low else None,
            },
            raw={"station": station_id, "predictions": predictions},
        )
