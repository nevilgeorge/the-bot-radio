from datetime import UTC, date, datetime, timedelta

import pytest

from the_bot_radio.web import view_helpers as h
from the_bot_radio.web.config import TZ
from the_bot_radio.web.models import Day, InputReading, Mood, Persona, Track


def et(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def reading(key: str, value: str, weight: float | None = 0.5, status: str = "ok") -> InputReading:
    return InputReading(
        key=key,
        label=key.title(),
        value=value,
        effect=f"→ {key} effect",
        weight=weight,
        status=status,
    )


def track(pos: int, ms: int, wildcard: bool = False) -> Track:
    return Track(
        position=pos,
        title=f"Song {pos}",
        artist="Artist",
        duration_ms=ms,
        spotify_id=f"id{pos}",
        reason="because",
        wildcard=wildcard,
    )


@pytest.fixture
def make_day():
    def factory(d: date = date(2026, 10, 26), inputs: list[InputReading] | None = None) -> Day:
        if inputs is None:
            inputs = [
                reading("weather", "52°F, overcast", 0.8),
                reading("g_train", "Running local", 0.6),
                reading("sunset", "6:01 PM", 0.3),
                reading("tide", "No data", None, status="skipped"),
                reading("dj_roll", "resident-01", None),
            ]
        return Day(
            date=d,
            dj="resident-01",
            title="Overcast, G running local",
            liner_notes=["First.", "Second."],
            inputs=inputs,
            mood=Mood(tempo_bpm=84, energy=3, brightness=4, obscurity=7),
            tracks=[track(1, 252_000), track(2, 588_000)],
            spotify_playlist_id="PLACEHOLDER",
            prompt="prompt",
            model="claude",
            generated_at=et(2026, 10, 26, 6, 0, 1),
        )

    return factory


# --- countdown ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(hours=17, minutes=49), "17h 49m"),
        (timedelta(hours=17, minutes=49, seconds=59), "17h 49m"),
        (timedelta(minutes=49), "49m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h 0m"),
        (timedelta(seconds=59), "0m"),
        (timedelta(0), "0m"),
        (timedelta(minutes=-5), "0m"),
        (timedelta(hours=25), "25h 0m"),
    ],
)
def test_format_countdown(delta, expected):
    assert h.format_countdown(delta) == expected


@pytest.mark.parametrize(
    ("now", "drop", "countdown"),
    [
        (et(2026, 10, 26, 5, 59), et(2026, 10, 26, 6, 0), "1m"),
        (et(2026, 10, 26, 5, 59, 30), et(2026, 10, 26, 6, 0), "0m"),
        (et(2026, 10, 26, 6, 0), et(2026, 10, 27, 6, 0), "24h 0m"),
        (et(2026, 10, 26, 6, 1), et(2026, 10, 27, 6, 0), "23h 59m"),
        (et(2026, 10, 26, 12, 11), et(2026, 10, 27, 6, 0), "17h 49m"),
        (et(2026, 12, 31, 23, 0), et(2027, 1, 1, 6, 0), "7h 0m"),
    ],
)
def test_next_drop(now, drop, countdown):
    result = h.next_drop(now)
    assert result == drop
    assert result.tzinfo is not None
    assert h.format_countdown(h.time_until_drop(now)) == countdown


def test_next_drop_fall_back():
    # 2026-11-01 is the DST end: 24h of wall clock, 25h actually elapse.
    now = et(2026, 10, 31, 6, 0)
    drop = h.next_drop(now)
    local = drop.astimezone(TZ)
    assert (local.date(), local.hour, local.minute) == (date(2026, 11, 1), 6, 0)
    assert local.utcoffset() == timedelta(hours=-5)
    assert drop.astimezone(UTC) - now.astimezone(UTC) == timedelta(hours=25)
    assert h.format_countdown(h.time_until_drop(now)) == "25h 0m"
    assert h.status_text(now).endswith("next drop in 25h 0m")


def test_next_drop_fall_back_from_7am():
    now = et(2026, 10, 31, 7, 0)  # EDT
    assert h.next_drop(now) == et(2026, 11, 1, 6, 0)  # EST
    assert h.format_countdown(h.time_until_drop(now)) == "24h 0m"


def test_next_drop_spring_forward():
    # 2026-03-08 is the DST start: only 23h elapse between the two 06:00s.
    now = et(2026, 3, 7, 6, 0)
    drop = h.next_drop(now)
    local = drop.astimezone(TZ)
    assert (local.date(), local.hour, local.minute) == (date(2026, 3, 8), 6, 0)
    assert local.utcoffset() == timedelta(hours=-4)
    assert h.format_countdown(h.time_until_drop(now)) == "23h 0m"


def test_next_drop_utc_input():
    now = datetime(2026, 10, 26, 9, 59, tzinfo=UTC)  # 05:59 EDT
    drop = h.next_drop(now)
    assert drop == et(2026, 10, 26, 6, 0)
    assert drop.astimezone(TZ).hour == 6
    assert h.format_countdown(h.time_until_drop(now)) == "1m"
    # 23:30 UTC on the 26th is already 19:30 ET on the 26th.
    assert h.next_drop(datetime(2026, 10, 26, 23, 30, tzinfo=UTC)) == et(2026, 10, 27, 6, 0)


def test_status_text():
    assert h.status_text(et(2026, 10, 26, 12, 11)) == ("Generated 06:00 ET · next drop in 17h 49m")
    assert h.status_text(et(2026, 10, 26, 5, 11)) == "Generated 06:00 ET · next drop in 49m"


def test_today_et():
    assert h.today_et(datetime(2026, 10, 27, 2, 0, tzinfo=UTC)) == date(2026, 10, 26)
    assert isinstance(h.today_et(), date)


def test_format_run_time():
    assert h.format_run_time(et(2026, 10, 26, 6, 0, 1)) == "06:00:01 ET"
    assert h.format_run_time(datetime(2026, 10, 26, 10, 0, 1, tzinfo=UTC)) == "06:00:01 ET"


# --- labels --------------------------------------------------------------------


def test_date_labels():
    d = date(2026, 10, 26)
    assert h.weekday_date(d) == "MON 10.26"
    assert h.long_date(d) == "MON 10.26.2026"
    assert h.short_date(d) == "10.26.2026"
    assert h.weekday_date(date(2026, 3, 1)) == "SUN 03.01"


def test_month_helpers():
    assert h.month_label("2026-10") == "OCTOBER 2026"
    assert h.month_label("2027-01") == "JANUARY 2027"
    assert h.month_name("2026-09") == "September"
    assert h.month_name("2026-12") == "December"


def test_resident_status():
    today = date(2026, 10, 26)
    assert h.resident_status(today, today) == h.ResidentStatus("On air today", True)
    assert h.resident_status(date(2026, 10, 25), today) == h.ResidentStatus(
        "Last aired on SUN 10.25", False
    )
    assert h.resident_status(None, today) == h.ResidentStatus("Not on air yet", False)


# --- durations -----------------------------------------------------------------


def test_format_duration():
    assert h.format_duration(252_000) == "4:12"
    assert h.format_duration(588_999) == "9:48"
    assert h.format_duration(5_000) == "0:05"


def test_total_duration():
    assert h.total_duration([track(1, 98 * 60_000)]) == "1 hr 38 min"
    assert h.total_duration([track(1, 30 * 60_000), track(2, 22 * 60_000 + 30_000)]) == "52 min"
    assert h.total_duration([]) == "0 min"


# --- mood ----------------------------------------------------------------------


def test_mood_meters():
    meters = h.mood_meters(Mood(tempo_bpm=84, energy=3, brightness=6, obscurity=7))
    assert [(m.label, m.value, m.pct, m.color) for m in meters] == [
        ("Tempo", "84 BPM avg", 24, "green"),
        ("Energy", "3 / 10", 30, "green"),
        ("Brightness", "6 / 10", 60, "amber"),
        ("Obscurity", "7 / 10", 70, "amber"),
    ]


@pytest.mark.parametrize(("bpm", "pct"), [(40, 0), (60, 0), (110, 50), (160, 100), (200, 100)])
def test_tempo_clamping(bpm, pct):
    assert h.mood_meters(Mood(tempo_bpm=bpm, energy=0, brightness=10, obscurity=0))[0].pct == pct


# --- stripes & tags ----------------------------------------------------------------


def test_stripe_cover(make_day):
    stripes = h.stripe_cover(make_day())
    # weather, g_train, sunset, skipped tide; dj_roll excluded
    assert [s.grow for s in stripes] == [80, 60, 30, 3]
    assert stripes[-1] == h.Stripe(3, "--line")
    assert stripes[0].color == h.stripe_color("weather", "52°F, overcast")
    assert all(s.color in h.PALETTE for s in stripes[:3])


def test_stripe_cover_deterministic(make_day):
    assert h.stripe_cover(make_day()) == h.stripe_cover(make_day())
    other = make_day(
        date(2026, 9, 1),
        [reading("sunset", "7:12 PM", 0.9), reading("weather", "52°F, overcast", 0.2)],
    )
    assert h.stripe_cover(other)[1].color == h.stripe_cover(make_day())[0].color


def test_stripe_cover_none_weight_excluded(make_day):
    day = make_day(inputs=[reading("weather", "x", None), reading("tide", "y", 0.5)])
    assert len(h.stripe_cover(day)) == 1


def test_stripe_color_uses_whole_palette():
    colors = {h.stripe_color("k", str(i)) for i in range(200)}
    assert colors == set(h.PALETTE)


def test_day_tags(make_day):
    assert h.day_tags(make_day()) == ["52°F, overcast", "Running local"]
    assert h.day_tags(make_day(), n=1) == ["52°F, overcast"]


def test_day_tags_tie_break_and_exclusions(make_day):
    day = make_day(
        inputs=[
            reading("dj_roll", "resident-01", None),
            reading("a", "A", 0.4),
            reading("b", "B", 0.9, status="stale"),
            reading("c", "C", 0.7),
            reading("d", "D", 0.4),
            reading("e", "E", 0.7),
        ]
    )
    assert h.day_tags(day) == ["C", "E"]
    assert h.day_tags(day, n=4) == ["C", "E", "A", "D"]


# --- personas & inputs -----------------------------------------------------------


def test_avatar_grid():
    grid = h.avatar_grid("resident-01")
    assert len(grid) == 4 and all(len(row) == 4 for row in grid)
    assert all(isinstance(cell, bool) for row in grid for cell in row)
    assert grid == h.avatar_grid("resident-01")
    assert grid != h.avatar_grid("resident-02")


def test_persona_names():
    personas = [Persona(id="resident-01", name="One"), Persona(id="resident-02", name="Two")]
    assert h.persona_names(personas) == {"resident-01": "One", "resident-02": "Two"}


@pytest.mark.parametrize("status", ["skipped", "stale"])
def test_input_display_quiet(status):
    assert h.input_display(reading("tide", "3.1 ft", 0.4, status)) == (
        "No reading",
        "→ skipped today",
    )


def test_input_display_ok():
    assert h.input_display(reading("tide", "3.1 ft", 0.4)) == ("3.1 ft", "→ tide effect")
