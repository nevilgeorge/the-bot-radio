"""Sunset for Greenpoint, computed locally with `astral` (no network).

Source: astral 3.x NOAA solar equations for ctx.settings.lat/lon on ctx.run_date,
in America/New_York. Cross-checked in tests against api.sunrise-sunset.org.
"""

from datetime import datetime, timedelta
from typing import Any, ClassVar

from astral import Observer
from astral.sun import SunDirection, golden_hour, sun

from .base import TZ, Reading, RunContext


def _round_minute(moment: datetime) -> datetime:
    """Round to the nearest whole minute (half-up)."""
    return (moment + timedelta(seconds=30)).replace(second=0, microsecond=0)


def format_clock(moment: datetime) -> str:
    """'5:59 PM' — 12-hour, no leading zero, rounded to the minute."""
    local_time = _round_minute(moment.astimezone(TZ))
    meridiem = "AM" if local_time.hour < 12 else "PM"
    return f"{local_time.hour % 12 or 12}:{local_time.minute:02d} {meridiem}"


def _to_local_iso(moment: datetime) -> str:
    """New York ISO string, to the second."""
    return moment.astimezone(TZ).isoformat(timespec="seconds")


class SunsetService:
    """Today's sunset time, plus sunrise, dusk, day length and golden hour."""

    key: ClassVar[str] = "sunset"
    label: ClassVar[str] = "Sunset"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Compute today's sun times for the configured lat/lon."""
        observer = Observer(latitude=ctx.settings.lat, longitude=ctx.settings.lon)
        # astral raises ValueError if the sun never rises/sets; the runner marks it "skipped".
        sun_times = sun(observer, ctx.run_date, tzinfo=TZ)
        day_length = sun_times["sunset"] - sun_times["sunrise"]
        data: dict[str, Any] = {
            "sunrise": _to_local_iso(sun_times["sunrise"]),
            "sunset": _to_local_iso(sun_times["sunset"]),
            "civil_dusk": _to_local_iso(sun_times["dusk"]),
            "day_length_min": round(day_length.total_seconds() / 60),
        }
        # Golden hour can be undefined near the solstices at high latitudes; it's optional.
        try:
            golden_hour_start, _ = golden_hour(
                observer, ctx.run_date, SunDirection.SETTING, tzinfo=TZ
            )
            data["golden_hour_start"] = _to_local_iso(golden_hour_start)
        except ValueError:
            pass
        raw = {"lat": ctx.settings.lat, "lon": ctx.settings.lon, "date": ctx.run_date.isoformat()}
        return ctx.reading(self, format_clock(sun_times["sunset"]), data=data, raw=raw)
