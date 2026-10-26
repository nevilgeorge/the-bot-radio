"""Pure formatting helpers used by the templates (exposed as the Jinja global `h`)."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from . import config
from .models import Day, InputReading, Mood, Persona, Track

# --- time & countdown -------------------------------------------------------


def _to_et(dt: datetime) -> datetime:
    """Convert to America/New_York; naive datetimes are assumed to already be ET."""
    return dt.replace(tzinfo=config.TZ) if dt.tzinfo is None else dt.astimezone(config.TZ)


def next_drop(now: datetime) -> datetime:
    """The next GENERATION_TIME in America/New_York strictly after `now` (aware, ET)."""
    local = _to_et(now)
    day = local.date()
    while True:
        candidate = datetime.combine(day, config.GENERATION_TIME, tzinfo=config.TZ)
        if candidate > local:
            return candidate
        day += timedelta(days=1)


def time_until_drop(now: datetime) -> timedelta:
    """Real elapsed time until the next drop (computed in UTC, so DST-correct)."""
    return next_drop(now).astimezone(UTC) - _to_et(now).astimezone(UTC)


def format_countdown(delta: timedelta) -> str:
    """'17h 49m', or '49m' under an hour. Minutes are floored; never negative."""
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def status_text(now: datetime) -> str:
    countdown = format_countdown(time_until_drop(now))
    return f"Generated {config.GENERATION_TIME_LABEL} · next drop in {countdown}"


def today_et(now: datetime | None = None) -> date:
    return _to_et(now or datetime.now(config.TZ)).date()


def format_run_time(dt: datetime) -> str:
    return _to_et(dt).strftime("%H:%M:%S ET")


# --- date labels ------------------------------------------------------------

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def weekday_date(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d:%m.%d}"


def long_date(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d:%m.%d.%Y}"


def short_date(d: date) -> str:
    return f"{d:%m.%d.%Y}"


def _parse_month(month: str) -> tuple[int, int]:
    year, mon = month.split("-")
    return int(year), int(mon)


def month_label(month: str) -> str:
    year, mon = _parse_month(month)
    return f"{_MONTHS[mon - 1].upper()} {year}"


def month_name(month: str) -> str:
    return _MONTHS[_parse_month(month)[1] - 1]


def last_played_label(d: date | None, today: date) -> str:
    if d is None:
        return "—"
    return "Today" if d == today else weekday_date(d)


# --- durations --------------------------------------------------------------


def format_duration(ms: int) -> str:
    minutes, seconds = divmod(max(0, ms) // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def total_duration(tracks: Iterable[Track]) -> str:
    minutes = sum(t.duration_ms for t in tracks) // 60_000
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min" if hours else f"{minutes} min"


# --- mood meters ------------------------------------------------------------


@dataclass(frozen=True)
class Meter:
    label: str
    value: str
    pct: int  # 0–100
    color: Literal["green", "amber"]


def _clamp_pct(fraction: float) -> int:
    return round(min(max(fraction, 0.0), 1.0) * 100)


def mood_meters(mood: Mood) -> list[Meter]:
    def score(label: str, value: int, color: Literal["green", "amber"]) -> Meter:
        return Meter(label, f"{value} / 10", _clamp_pct(value / 10), color)

    return [
        Meter(
            "Tempo",
            f"{mood.tempo_bpm} BPM avg",
            _clamp_pct((mood.tempo_bpm - 60) / 100),
            "green",
        ),
        score("Energy", mood.energy, "green"),
        score("Brightness", mood.brightness, "amber"),
        score("Obscurity", mood.obscurity, "amber"),
    ]


# --- stripe cover art -------------------------------------------------------

PALETTE = ["--green", "--amber", "--slate", "--text", "--ink", "--blue"]
SKIPPED_STRIPE_GROW = 3
SKIPPED_STRIPE_COLOR = "--line"


@dataclass(frozen=True)
class Stripe:
    grow: int
    color: str  # CSS custom property name, e.g. "--green"


def _md5_int(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest(), 16)


def stripe_color(key: str, value: str) -> str:
    """The one place the stripe color rule lives: same reading → same color, always."""
    return PALETTE[_md5_int(f"{key}:{value}") % len(PALETTE)]


def _cover_inputs(day: Day) -> list[InputReading]:
    return [r for r in day.inputs if r.key != "dj_roll"]


def stripe_cover(day: Day) -> list[Stripe]:
    stripes: list[Stripe] = []
    for r in _cover_inputs(day):
        if r.status != "ok":
            stripes.append(Stripe(SKIPPED_STRIPE_GROW, SKIPPED_STRIPE_COLOR))
        elif r.weight is not None:
            stripes.append(Stripe(round(r.weight * 100), stripe_color(r.key, r.value)))
    return stripes


def day_tags(day: Day, n: int = 2) -> list[str]:
    """Values of the n highest-weight ok inputs; ties keep display order (stable sort)."""
    ok = [r for r in _cover_inputs(day) if r.status == "ok" and r.weight is not None]
    return [r.value for r in sorted(ok, key=lambda r: -r.weight)[:n]]


# --- personas ---------------------------------------------------------------


def avatar_grid(persona_id: str) -> list[list[bool]]:
    """Deterministic 4×4 on/off pattern from a hash of the persona id."""
    bits = int(hashlib.sha256(persona_id.encode()).hexdigest(), 16)
    return [[bool(bits >> (row * 4 + col) & 1) for col in range(4)] for row in range(4)]


def persona_names(personas: Iterable[Persona]) -> dict[str, str]:
    return {p.id: p.name for p in personas}


# --- inputs -----------------------------------------------------------------


def input_display(reading: InputReading) -> tuple[str, str]:
    if reading.status != "ok":
        return "No reading", "→ skipped today"
    return reading.value, reading.effect
