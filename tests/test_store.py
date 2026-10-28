import json
import logging
import os
from datetime import date
from pathlib import Path

import pytest

from the_bot_radio.web.store import Store

REPO_DATA = Path(__file__).resolve().parents[1] / "data"


def make_day(d: str, dj: str = "resident-01", title: str = "A day") -> dict:
    return {
        "date": d,
        "dj": dj,
        "title": title,
        "liner_notes": ["Notes."],
        "inputs": [
            {
                "key": "dj_roll",
                "label": "The DJ roll",
                "value": "[Resident 01]",
                "effect": "→ decides",
                "weight": None,
            },
            {
                "key": "weather",
                "label": "Weather · Greenpoint",
                "value": "52°F, overcast",
                "effect": "→ slower",
                "weight": 0.25,
            },
        ],
        "mood": {"tempo_bpm": 84, "energy": 3, "brightness": 2, "obscurity": 7},
        "tracks": [
            {
                "position": 1,
                "title": "[Track title]",
                "artist": "[Artist]",
                "duration_ms": 252000,
                "spotify_id": "PLACEHOLDER",
                "reason": "Because.",
            }
        ],
        "spotify_playlist_id": "PLACEHOLDER",
        "prompt": "Prompt.",
        "model": "[MODEL]",
        "generated_at": f"{d}T06:00:01-04:00",
    }


def write_day(data_dir: Path, d: str, **kw) -> Path:
    path = data_dir / "days" / f"{d}.json"
    path.write_text(json.dumps(make_day(d, **kw)), encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "days").mkdir()
    write_day(tmp_path, "2026-09-29", dj="resident-01")
    write_day(tmp_path, "2026-09-30", dj="resident-02")
    write_day(tmp_path, "2026-10-01", dj="resident-01")
    write_day(tmp_path, "2026-10-02", dj="resident-03")
    personas = [
        {"id": f"resident-0{i}", "name": f"[Resident 0{i}]", "bio": ""} for i in range(1, 5)
    ]
    (tmp_path / "personas.json").write_text(json.dumps(personas), encoding="utf-8")
    inputs = [
        {"key": "weather", "name": "Weather", "source": "NWS", "reads": "r", "effect": "e"},
        {"key": "g_train", "name": "The G train", "source": "MTA", "reads": "r", "effect": "e"},
    ]
    (tmp_path / "inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
    return tmp_path


def test_construction_is_lazy(tmp_path: Path):
    store = Store(tmp_path / "does-not-exist")
    assert store.latest_day() is None
    assert store.list_days() == []
    assert store.personas() == []
    assert store.input_descriptions() == []


def test_loads_and_sorts_newest_first(data_dir: Path):
    store = Store(data_dir)
    days = store.list_days()
    assert [d.date.isoformat() for d in days] == [
        "2026-10-02",
        "2026-10-01",
        "2026-09-30",
        "2026-09-29",
    ]
    assert store.latest_day().date == date(2026, 10, 2)
    assert store.get_day(date(2026, 9, 30)).dj == "resident-02"
    assert store.get_day(date(2026, 1, 1)) is None


def test_list_days_filters(data_dir: Path):
    store = Store(data_dir)
    assert [d.date.day for d in store.list_days(dj="resident-01")] == [1, 29]
    assert [d.date.day for d in store.list_days(month="2026-09")] == [30, 29]
    assert [d.date.day for d in store.list_days(dj="resident-01", month="2026-10")] == [1]
    assert store.list_days(dj="nobody") == []


def test_months(data_dir: Path):
    store = Store(data_dir)
    assert store.months() == ["2026-10", "2026-09"]
    assert store.months(dj="resident-02") == ["2026-09"]
    assert store.months(dj="resident-04") == []


def test_prev_next(data_dir: Path):
    store = Store(data_dir)
    prev, nxt = store.prev_next(date(2026, 10, 1))
    assert prev.date == date(2026, 9, 30)
    assert nxt.date == date(2026, 10, 2)

    prev, nxt = store.prev_next(date(2026, 10, 2))  # newest
    assert prev.date == date(2026, 10, 1)
    assert nxt is None

    prev, nxt = store.prev_next(date(2026, 9, 29))  # oldest
    assert prev is None
    assert nxt.date == date(2026, 9, 30)


def test_personas_and_inputs(data_dir: Path):
    store = Store(data_dir)
    assert [p.id for p in store.personas()] == [f"resident-0{i}" for i in range(1, 5)]
    assert store.persona("resident-03").name == "[Resident 03]"
    assert store.persona("nobody") is None
    assert [i.key for i in store.input_descriptions()] == ["weather", "g_train"]


def test_resident_stats(data_dir: Path):
    store = Store(data_dir)
    assert store.resident_stats("resident-01") == (2, date(2026, 10, 1))
    assert store.resident_stats("resident-03") == (1, date(2026, 10, 2))
    assert store.resident_stats("resident-04") == (0, None)


def test_invalid_files_are_skipped(data_dir: Path, caplog: pytest.LogCaptureFixture):
    (data_dir / "days" / "2026-10-03.json").write_text("{not json", encoding="utf-8")
    bad = make_day("2026-10-04")
    del bad["mood"]
    (data_dir / "days" / "2026-10-04.json").write_text(json.dumps(bad), encoding="utf-8")

    store = Store(data_dir)
    with caplog.at_level(logging.WARNING, logger="the_bot_radio.web.store"):
        days = store.list_days()

    assert len(days) == 4
    assert store.latest_day().date == date(2026, 10, 2)
    messages = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "2026-10-03.json" in messages
    assert "2026-10-04.json" in messages


def test_invalid_personas_file_does_not_raise(data_dir: Path, caplog: pytest.LogCaptureFixture):
    (data_dir / "personas.json").write_text("[", encoding="utf-8")
    store = Store(data_dir)
    with caplog.at_level(logging.WARNING, logger="the_bot_radio.web.store"):
        assert store.personas() == []
    assert any("personas.json" in r.getMessage() for r in caplog.records)


def test_hot_reload(data_dir: Path):
    store = Store(data_dir)
    assert store.latest_day().date == date(2026, 10, 2)

    path = write_day(data_dir, "2026-10-03", dj="resident-04", title="New drop")
    assert store.latest_day().date == date(2026, 10, 3)
    assert store.latest_day().title == "New drop"

    # Rewriting an existing file is picked up via its mtime.
    path.write_text(
        json.dumps(make_day("2026-10-03", dj="resident-04", title="Edited")), encoding="utf-8"
    )
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert store.latest_day().title == "Edited"

    path.unlink()
    assert store.latest_day().date == date(2026, 10, 2)


def test_real_data_dir_validates(caplog: pytest.LogCaptureFixture):
    store = Store(REPO_DATA)
    with caplog.at_level(logging.WARNING, logger="the_bot_radio.web.store"):
        days = store.list_days()
    assert not caplog.records
    assert len(days) == 12
    assert days[0].date == date(2026, 10, 26)
    assert len(store.personas()) == 7
    assert len(store.input_descriptions()) == 8
    keys = [i.key for i in store.input_descriptions()]
    for day in days:
        assert [r.key for r in day.inputs] == keys
        assert store.persona(day.dj) is not None


def test_pending_days_are_hidden_from_all_store_views(data_dir: Path) -> None:
    pending_day = make_day("2026-10-03", dj="resident-04")
    pending_day.update(publication_status="pending", spotify_playlist_id=None)
    (data_dir / "days" / "2026-10-03.json").write_text(json.dumps(pending_day))
    store = Store(data_dir)
    assert store.latest_day().date == date(2026, 10, 2)
    assert store.get_day(date(2026, 10, 3)) is None
    assert len(store.list_days()) == 4
    assert store.list_days(dj="resident-04") == []
    assert store.resident_stats("resident-04") == (0, None)
    assert store.prev_next(date(2026, 10, 2))[1] is None


def test_pending_day_becomes_visible_after_publication_without_restart(data_dir: Path) -> None:
    pending_day = make_day("2026-11-01", dj="resident-04")
    pending_day.update(publication_status="pending", spotify_playlist_id=None)
    path = data_dir / "days" / "2026-11-01.json"
    path.write_text(json.dumps(pending_day))
    store = Store(data_dir)
    assert "2026-11" not in store.months()
    assert store.get_day(date(2026, 11, 1)) is None

    pending_day.update(publication_status="published", spotify_playlist_id="1" * 22)
    path.write_text(json.dumps(pending_day))
    file_stat = path.stat()
    os.utime(path, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns + 1_000_000_000))

    assert store.latest_day().date == date(2026, 11, 1)
    assert store.get_day(date(2026, 11, 1)).spotify_playlist_id == "1" * 22
    assert store.months()[0] == "2026-11"
    assert store.resident_stats("resident-04") == (1, date(2026, 11, 1))
    assert store.prev_next(date(2026, 10, 2))[1].date == date(2026, 11, 1)


def test_explicitly_published_day_without_playlist_id_is_hidden(data_dir: Path) -> None:
    invalid_day = make_day("2026-10-03")
    invalid_day.update(publication_status="published", spotify_playlist_id=None)
    (data_dir / "days" / "2026-10-03.json").write_text(json.dumps(invalid_day))
    assert Store(data_dir).get_day(date(2026, 10, 3)) is None


def test_snapshot_source_preserves_all_views_and_local_static_reload(data_dir: Path) -> None:
    """All public views use memory while personas and input descriptions stay local."""
    from unittest.mock import Mock

    from the_bot_radio.web.models import Day

    records = {
        date.fromisoformat(day_date): Day.model_validate(make_day(day_date, dj=dj))
        for day_date, dj in [
            ("2026-09-29", "resident-01"),
            ("2026-10-01", "resident-02"),
            ("2026-10-03", "resident-01"),
        ]
    }
    source = Mock()
    source.days.side_effect = lambda: records.copy()
    store = Store(data_dir, daily_source=source)
    store._days_signature = Mock(side_effect=AssertionError("local daily files inspected"))
    assert store.latest_day().date == date(2026, 10, 3)
    assert store.get_day(date(2026, 10, 2)) is None
    assert [day.date.day for day in store.list_days()] == [3, 1, 29]
    assert [day.date.day for day in store.list_days(dj="resident-01", month="2026-10")] == [3]
    assert store.months() == ["2026-10", "2026-09"]
    assert store.months(dj="resident-02") == ["2026-10"]
    previous, following = store.prev_next(date(2026, 10, 1))
    assert previous.date == date(2026, 9, 29)
    assert following.date == date(2026, 10, 3)
    assert store.resident_stats("resident-01") == (2, date(2026, 10, 3))
    assert store.persona("resident-03") is not None
    assert len(store.input_descriptions()) == 2
    (data_dir / "personas.json").write_text("[]")
    (data_dir / "inputs.json").write_text("[]")
    assert store.personas() == []
    assert store.input_descriptions() == []
    records.clear()
    assert store.latest_day() is None
    assert store.list_days() == []
