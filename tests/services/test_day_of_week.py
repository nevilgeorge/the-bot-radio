from datetime import UTC, date, datetime

import pytest

from the_bot_radio.services.inputs.day_of_week import DayOfWeekService, federal_holidays

pytestmark = pytest.mark.anyio


async def test_monday(make_ctx):
    r = await DayOfWeekService().fetch(make_ctx(date(2026, 10, 26)))
    assert (r.key, r.label, r.value, r.status) == ("day_of_week", "Day of week", "Monday", "ok")
    assert r.data == {
        "weekday": "Monday",
        "iso_weekday": 1,
        "is_weekend": False,
        "date": "2026-10-26",
        "holiday": None,
    }
    assert r.raw == {}


@pytest.mark.parametrize(
    ("d", "weekday", "weekend"),
    [
        (date(2026, 10, 31), "Saturday", True),
        (date(2026, 11, 1), "Sunday", True),  # DST ends
        (date(2026, 3, 8), "Sunday", True),  # DST starts
        (date(2026, 10, 30), "Friday", False),
    ],
)
async def test_weekend_flags(make_ctx, d, weekday, weekend):
    r = await DayOfWeekService().fetch(make_ctx(d))
    assert r.value == weekday
    assert r.data["is_weekend"] is weekend


async def test_uses_run_date_not_now(make_ctx):
    # 03:30 UTC on Tuesday is still Monday evening in New York.
    ctx = make_ctx(date(2026, 10, 26), now=datetime(2026, 10, 27, 3, 30, tzinfo=UTC))
    r = await DayOfWeekService().fetch(ctx)
    assert r.value == "Monday"
    assert r.data["date"] == "2026-10-26"


@pytest.mark.parametrize(
    ("d", "name"),
    [
        (date(2026, 11, 26), "Thanksgiving Day"),
        (date(2026, 7, 3), "Independence Day"),  # July 4 2026 is a Saturday
        (date(2026, 1, 19), "Martin Luther King Jr. Day"),
        (date(2026, 5, 25), "Memorial Day"),
        (date(2026, 9, 7), "Labor Day"),
        (date(2026, 10, 12), "Columbus Day"),
        (date(2026, 11, 11), "Veterans Day"),
        (date(2026, 12, 25), "Christmas Day"),
        (date(2027, 12, 31), "New Year's Day"),  # Jan 1 2028 is a Saturday
        (date(2028, 1, 1), None),
        (date(2026, 7, 4), None),
    ],
)
async def test_holidays(make_ctx, d, name):
    r = await DayOfWeekService().fetch(make_ctx(d))
    assert r.data["holiday"] == name
    assert r.value == d.strftime("%A")  # holiday never replaces the weekday


def test_eleven_holidays_a_year():
    assert len(federal_holidays(2026)) == 11
    assert len(federal_holidays(2028)) == 10  # its New Year's was observed in 2027
