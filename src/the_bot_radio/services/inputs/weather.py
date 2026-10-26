"""Weather for Greenpoint from the National Weather Service API (api.weather.gov, no key).

GET /points/{lat},{lon} -> properties.forecastHourly + properties.forecast, then both
forecasts concurrently (3 calls total). The hourly forecast gives "now" (temp, conditions,
wind) and the max precip chance over the next 12 hours; the daily forecast gives today's
high and tonight's low.
"""

import asyncio
import re
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from .base import TZ, Reading, RunContext

API = "https://api.weather.gov"
HEADERS = {"Accept": "application/geo+json"}
RETRY_DELAY = 0.5  # seconds before the single retry on a 5xx
STALE_AFTER = timedelta(hours=3)  # first hourly period older than this -> "stale"
PRECIP_HOURS = 12
MAX_CONDITION_LEN = 22  # keeps "52°F, <condition>" chip-sized

# Applied in order to the lowercased first clause of shortForecast.
_PHRASE_REPLACEMENTS: list[tuple[str, str]] = [
    (r"\b(slight )?chance ", "chance of "),
    (r"(showers and )?thunderstorms", "t-storms"),
    (r"rain showers", "showers"),
    (r"snow showers", "snow"),
    (r"areas of ", ""),
    (r"patchy ", ""),
    (r"isolated ", ""),
    (r"scattered ", ""),
]
# Dropped one at a time (most to least expendable) while the condition is still too long.
_DROPPABLE_QUALIFIERS = ["likely", "chance of", "light", "heavy", "very", "mostly", "partly"]


def shorten_condition(short_forecast: str) -> str:
    """'Slight Chance Rain Showers then Sunny' -> 'chance of showers'."""
    first_clause = re.split(r"\s+then\s+", short_forecast.strip(), maxsplit=1, flags=re.I)[0]
    condition = first_clause.lower()
    for pattern, replacement in _PHRASE_REPLACEMENTS:
        condition = re.sub(pattern, replacement, condition)
    for qualifier in _DROPPABLE_QUALIFIERS:
        if len(condition) <= MAX_CONDITION_LEN:
            break
        condition = re.sub(rf"\b{qualifier}\b", "", condition)
        condition = " ".join(condition.split())
    # Still too long: keep the last two words, which carry the core condition.
    if len(condition) > MAX_CONDITION_LEN:
        condition = " ".join(condition.split()[-2:])
    return " ".join(condition.split())


def parse_wind_mph(wind_speed: str) -> int:
    """'10 to 15 mph' -> 15 (the upper bound); '7 mph' -> 7; '' / 'Calm' -> 0."""
    speeds_mph = [int(number) for number in re.findall(r"\d+", wind_speed)]
    return max(speeds_mph, default=0)


async def _get_json(ctx: RunContext, url: str) -> dict[str, Any]:
    """GET with one short retry on 5xx (NWS gridpoint endpoints blip); then raise."""
    response = await ctx.http.get(url, headers=HEADERS)
    if response.status_code >= 500:
        await asyncio.sleep(RETRY_DELAY)
        response = await ctx.http.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _forecast_periods(forecast_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A forecast's ``properties.periods`` list, raising ValueError if it's missing or empty."""
    periods = forecast_payload.get("properties", {}).get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("NWS forecast has no periods")
    return periods


def _period_start(period: dict[str, Any]) -> datetime:
    """A forecast period's start time as an aware datetime."""
    return datetime.fromisoformat(period["startTime"])


def _precip_chance(period: dict[str, Any]) -> Any:
    """A period's probabilityOfPrecipitation value (may be None)."""
    return (period.get("probabilityOfPrecipitation") or {}).get("value")


def _temperature_for_day(
    daily_periods: list[dict[str, Any]], day: date, is_daytime: bool
) -> int | None:
    """Temperature of `day`'s daytime (high) or nighttime (low) period, if present."""
    return next(
        (
            int(period["temperature"])
            for period in daily_periods
            if period["isDaytime"] == is_daytime
            and _period_start(period).astimezone(TZ).date() == day
        ),
        None,
    )


def _trim_period(period: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields of a forecast period worth storing in `raw`."""
    kept_keys = ("startTime", "endTime", "name", "isDaytime", "temperature", "shortForecast")
    trimmed_period = {key: period[key] for key in kept_keys if key in period}
    trimmed_period.update(
        precip=_precip_chance(period),
        windSpeed=period.get("windSpeed"),
        windDirection=period.get("windDirection"),
    )
    return trimmed_period


class WeatherService:
    """Current conditions plus today's high/low and precip chance."""

    key: ClassVar[str] = "weather"
    label: ClassVar[str] = "Weather · Greenpoint"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Resolve the NWS grid for our lat/lon, then read the hourly and daily forecasts."""
        lat, lon = round(ctx.settings.lat, 4), round(ctx.settings.lon, 4)
        points_payload = await _get_json(ctx, f"{API}/points/{lat},{lon}")
        try:
            hourly_url = points_payload["properties"]["forecastHourly"]
            daily_url = points_payload["properties"]["forecast"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"NWS /points missing forecast URLs: {exc}") from exc
        hourly_payload, daily_payload = await asyncio.gather(
            _get_json(ctx, hourly_url), _get_json(ctx, daily_url)
        )
        hourly_periods = _forecast_periods(hourly_payload)
        daily_periods = _forecast_periods(daily_payload)

        try:
            # The period covering "now" (NWS normally starts at the current hour).
            current_period = next(
                (
                    period
                    for period in hourly_periods
                    if datetime.fromisoformat(period["endTime"]) > ctx.now
                ),
                hourly_periods[-1],
            )
            current_start = _period_start(current_period)
            precip_horizon = current_start + timedelta(hours=PRECIP_HOURS)
            upcoming_periods = [
                period
                for period in hourly_periods
                if current_start <= _period_start(period) < precip_horizon
            ]
            precip_chance = max(int(_precip_chance(period) or 0) for period in upcoming_periods)
            temp_f = int(current_period["temperature"])
            short_forecast = str(current_period["shortForecast"])
            wind_speed = str(current_period["windSpeed"])
            wind_direction = current_period.get("windDirection") or None
            high_f = _temperature_for_day(daily_periods, ctx.run_date, is_daytime=True)
            low_f = _temperature_for_day(daily_periods, ctx.run_date, is_daytime=False)
            first_period_start = _period_start(hourly_periods[0])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed NWS forecast: {exc!r}") from exc

        conditions = shorten_condition(short_forecast)
        is_stale = ctx.now - first_period_start > STALE_AFTER
        return ctx.reading(
            self,
            f"{temp_f}°F, {conditions}",
            status="stale" if is_stale else "ok",
            data={
                "temp_f": temp_f,
                "conditions": conditions,
                "short_forecast": short_forecast,
                "precip_chance": precip_chance,
                "wind_mph": parse_wind_mph(wind_speed),
                "wind_dir": wind_direction,
                "high_f": high_f,
                "low_f": low_f,
                "forecast_period_start": current_start.isoformat(),
                "forecast_updated": hourly_payload["properties"].get("updateTime"),
            },
            raw={
                "grid": {
                    key: points_payload["properties"].get(key)
                    for key in ("gridId", "gridX", "gridY")
                },
                "hourly": [_trim_period(period) for period in upcoming_periods],
                "daily": [_trim_period(period) for period in daily_periods[:2]],
            },
        )
