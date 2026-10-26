"""Day of week, derived from `ctx.run_date` (the America/New_York date). No external source.

Also flags US federal holidays (observed dates) from a small hard-coded rule set:
fixed-date holidays shift Sat→Fri / Sun→Mon; the rest are nth-weekday rules.
Inauguration Day (DC-area only, every four years) is deliberately left out.
"""

from datetime import date, timedelta
from typing import ClassVar

from .base import Reading, RunContext

MON, THU = 0, 3
SATURDAY, SUNDAY = 5, 6

# (name, month, day)
_FIXED_DATE_HOLIDAYS = (
    ("New Year's Day", 1, 1),
    ("Juneteenth", 6, 19),
    ("Independence Day", 7, 4),
    ("Veterans Day", 11, 11),
    ("Christmas Day", 12, 25),
)

# (name, month, weekday, n) — n = -1 means the last such weekday of the month
_NTH_WEEKDAY_HOLIDAYS = (
    ("Martin Luther King Jr. Day", 1, MON, 3),
    ("Washington's Birthday", 2, MON, 3),
    ("Memorial Day", 5, MON, -1),
    ("Labor Day", 9, MON, 1),
    ("Columbus Day", 10, MON, 2),
    ("Thanksgiving Day", 11, THU, 4),
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth `weekday` (0 = Monday) of the month; n = -1 gives the last one."""
    if n > 0:
        first_of_month = date(year, month, 1)
        days_to_first_match = (weekday - first_of_month.weekday()) % 7
        return first_of_month + timedelta(days=days_to_first_match + 7 * (n - 1))
    first_of_next_month = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    last_of_month = first_of_next_month - timedelta(days=1)
    return last_of_month - timedelta(days=(last_of_month.weekday() - weekday) % 7)


def _observed_date(holiday_date: date) -> date:
    """Shift a weekend holiday to its observed weekday: Saturday → Friday, Sunday → Monday."""
    weekend_shift_days = {SATURDAY: -1, SUNDAY: 1}.get(holiday_date.weekday(), 0)
    return holiday_date + timedelta(days=weekend_shift_days)


def federal_holidays(year: int) -> dict[date, str]:
    """Observed federal holiday dates that fall within `year`."""
    holidays_by_date = {
        _nth_weekday(year, month, weekday, n): holiday_name
        for holiday_name, month, weekday, n in _NTH_WEEKDAY_HOLIDAYS
    }
    # Include next year's New Year's Day: when Jan 1 is a Saturday it's observed on Dec 31.
    for candidate_year in (year, year + 1):
        for holiday_name, month, day in _FIXED_DATE_HOLIDAYS:
            observed_date = _observed_date(date(candidate_year, month, day))
            if observed_date.year == year:
                holidays_by_date[observed_date] = holiday_name
    return holidays_by_date


class DayOfWeekService:
    """Today's weekday, weekend flag, and federal holiday (if any)."""

    key: ClassVar[str] = "day_of_week"
    label: ClassVar[str] = "Day of week"

    async def fetch(self, ctx: RunContext) -> Reading:
        """Describe `ctx.run_date`; needs no network."""
        run_date = ctx.run_date
        weekday_name = run_date.strftime("%A")
        data = {
            "weekday": weekday_name,
            "iso_weekday": run_date.isoweekday(),
            "is_weekend": run_date.isoweekday() >= 6,
            "date": run_date.isoformat(),
            "holiday": federal_holidays(run_date.year).get(run_date),
        }
        return ctx.reading(self, weekday_name, data=data)
