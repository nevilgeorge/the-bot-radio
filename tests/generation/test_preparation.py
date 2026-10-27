"""Offline preparation coverage for frozen evidence and trusted run references."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from the_bot_radio.catalog import CatalogTrack
from the_bot_radio.generation import preparation
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.services.inputs.base import TZ, Reading
from the_bot_radio.web.models import Mood

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 6, tzinfo=TZ)


def write_sources(data_dir: Path, *, artist_count: int = 6) -> None:
    """Write domain fixtures without making provider requests."""
    data_dir.mkdir(parents=True, exist_ok=True)
    tracks = [
        CatalogTrack(
            spotify_id=f"{index:022d}",
            title=f"Song {index}",
            artists=[f"Artist {index % artist_count}"],
            album="Album",
            duration_ms=180000,
            spotify_url=f"https://open.spotify.com/track/{index:022d}",
        )
        for index in range(6)
    ]
    (data_dir / "shared_catalog.jsonl").write_text(
        "".join(track.model_dump_json() + "\n" for track in tracks)
    )
    (data_dir / "personas.json").write_text(
        json.dumps(
            [
                {
                    "id": f"resident-{index}",
                    "name": f"Resident {index}",
                    "bio": f"Musical biography {index}",
                    "agent_instructions": f"Choose songs with character {index}",
                    "speaking_voice": f"Concise warm voice {index}",
                }
                for index in range(2)
            ]
        )
    )


def published_record(playlist_date: date, dj_id: str = "resident-0") -> dict:
    """Build a published domain record whose tracks are outside the fixture catalog."""
    payload = {
        "date": playlist_date.isoformat(),
        "dj": dj_id,
        "title": "Previous set",
        "description": "Earlier songs",
        "liner_notes": ["A previous day."],
        "tracks": [
            {
                "position": index + 1,
                "spotify_id": f"{100 + index:022d}",
                "spotify_url": f"https://open.spotify.com/track/{100 + index:022d}",
                "title": f"Older song {index}",
                "artists": [f"Older artist {index}"],
                "artist": f"Older artist {index}",
                "duration_ms": 180000,
                "reason": "It fits.",
                "wildcard": index >= 5,
            }
            for index in range(10)
        ],
        "inputs": [],
        "mood": {"tempo_bpm": 90, "energy": 4, "brightness": 3, "obscurity": 7},
        "publication_status": "published",
        "spotify_playlist_id": "p" * 22,
        "prompt": "Previous prompt",
        "model": "test-model",
        "generated_at": NOW.isoformat(),
    }
    DailyRecord.model_validate(payload)
    return payload


def write_day(data_dir: Path, playlist_date: date, payload: dict) -> None:
    """Persist one history fixture by date."""
    days_dir = data_dir / "days"
    days_dir.mkdir(exist_ok=True)
    (days_dir / f"{playlist_date.isoformat()}.json").write_text(json.dumps(payload))


@pytest.fixture
def fake_readings(monkeypatch: pytest.MonkeyPatch) -> list[Reading]:
    """Replace neighborhood collection with stale and skipped source evidence."""
    readings = [
        Reading(
            key="weather",
            label="Weather",
            value="Rain",
            status="stale",
            data={"temp_f": 52},
            raw={"forecast": "retained"},
            fetched_at=NOW,
        ),
        Reading(
            key="g_train",
            label="G",
            value="",
            status="skipped",
            error="TimeoutError",
            fetched_at=NOW,
        ),
    ]
    monkeypatch.setattr(preparation.runner, "read_all", AsyncMock(return_value=readings))
    return readings


async def prepare(
    data_dir: Path,
    artifact_dir: Path,
    *,
    now: datetime = NOW,
    run_date: date = RUN_DATE,
) -> tuple[preparation.PreparedRun, SimpleNamespace]:
    """Run preparation with a fake mood service and offline HTTP client."""
    mood_service = SimpleNamespace(
        score_mood=AsyncMock(return_value=Mood(tempo_bpm=90, energy=4, brightness=3, obscurity=7))
    )
    async with httpx.AsyncClient() as client:
        result = await preparation.prepare_run(
            data_dir, artifact_dir, run_date, now, client, mood_service
        )
    return result, mood_service


async def test_snapshot_history_and_selected_persona_only(tmp_path, fake_readings):
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    write_day(
        data_dir, RUN_DATE - timedelta(days=30), published_record(RUN_DATE - timedelta(days=30))
    )
    write_day(
        data_dir, RUN_DATE - timedelta(days=1), published_record(RUN_DATE - timedelta(days=1))
    )
    write_day(
        data_dir, RUN_DATE - timedelta(days=31), published_record(RUN_DATE - timedelta(days=31))
    )
    write_day(data_dir, RUN_DATE, published_record(RUN_DATE))
    write_day(data_dir, RUN_DATE - timedelta(days=2), {"dj": "fixture"})
    write_day(data_dir, RUN_DATE - timedelta(days=3), {"publication_status": "pending"})
    catalog_source = data_dir / "shared_catalog.jsonl"
    # Preserve valid source formatting, rather than serializing normalized model records.
    original_catalog = "".join(
        "  " + line + " \n" for line in catalog_source.read_text().splitlines()
    )
    catalog_source.write_text(original_catalog)
    result, mood_service = await prepare(data_dir, tmp_path / "run")
    assert (result.workspace / "catalog.jsonl").read_bytes() == catalog_source.read_bytes()
    assert result.persona.id == "resident-1"
    assert len(result.context.recent_track_ids) == 10
    assert len(list((result.workspace / "history").iterdir())) == 2
    context_path = result.workspace / "context.json"
    context = json.loads(context_path.read_text())
    assert context["history"]["incomplete"]
    assert len(context["history"]["included_dates"]) == 2
    assert context["readings"][0]["data"] == {"persona_id": "resident-1"}
    assert context["readings"][1]["status"] == "stale"
    assert context["readings"][2]["status"] == "skipped"
    assert "raw" not in context["readings"][1]
    diagnostics = json.loads((result.artifact_dir / "diagnostics.json").read_text())
    audit = diagnostics["inputs"]
    assert audit[1]["raw"] == {"forecast": "retained"}
    assert audit[2]["error"] == "TimeoutError"
    assert not list(result.artifact_dir.rglob("personas.json"))
    assert not (result.artifact_dir / "setup").exists()
    assert {path.name for path in result.workspace.iterdir()} == {
        "catalog.jsonl",
        "context.json",
        "history",
        "schemas",
    }
    prompt_data = json.loads(result.prompt)
    assert prompt_data == {"persona": result.persona.model_dump()}
    assert "Musical biography 1" in result.prompt
    assert "Musical biography 0" not in result.prompt
    assert not (result.workspace / "schemas/daily_playlist.schema.json").exists()
    assert (result.workspace / "schemas/playlist_proposal.schema.json").exists()
    assert mood_service.score_mood.await_count == 1
    assert diagnostics["mood_usage"] == {}
    (data_dir / "shared_catalog.jsonl").write_text("")
    assert len(preparation.load_catalog(result.workspace / "catalog.jsonl")) == 6


async def test_pending_yesterday_does_not_exclude_its_dj(tmp_path, fake_readings):
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    first_run, _ = await prepare(data_dir, tmp_path / "first")
    write_day(
        data_dir,
        RUN_DATE - timedelta(days=1),
        {
            "publication_status": "pending",
            "dj": first_run.persona.id,
        },
    )
    second_run, _ = await prepare(data_dir, tmp_path / "second")
    assert second_run.persona.id == first_run.persona.id


@pytest.mark.parametrize("artist_count", [1, 4])
async def test_insufficient_distinct_artists_fails(tmp_path, fake_readings, artist_count):
    write_sources(tmp_path / "data", artist_count=artist_count)
    with pytest.raises(ValueError, match="five eligible"):
        await prepare(tmp_path / "data", tmp_path / "run")


async def test_missing_catalog_fails(tmp_path, fake_readings):
    with pytest.raises(ValueError, match="catalog is missing"):
        await prepare(tmp_path / "data", tmp_path / "run")


async def test_naive_timestamp_fails(tmp_path, fake_readings):
    with pytest.raises(ValueError, match="timezone-aware"):
        await prepare(tmp_path / "data", tmp_path / "run", now=datetime(2026, 10, 27))


async def test_malformed_published_history_fails(tmp_path, fake_readings):
    write_sources(tmp_path / "data")
    write_day(tmp_path / "data", RUN_DATE - timedelta(days=1), {"publication_status": "published"})
    with pytest.raises(ValueError):
        await prepare(tmp_path / "data", tmp_path / "run")


async def test_history_filename_date_mismatch_fails(tmp_path, fake_readings):
    write_sources(tmp_path / "data")
    write_day(
        tmp_path / "data",
        RUN_DATE - timedelta(days=1),
        published_record(RUN_DATE - timedelta(days=2)),
    )
    with pytest.raises(ValueError, match="date does not match filename"):
        await prepare(tmp_path / "data", tmp_path / "run")


@pytest.mark.parametrize(
    "hour, expected_offset, expected_fold", [(5, "-04:00", 0), (6, "-05:00", 1)]
)
async def test_dst_fall_back_keeps_aware_instant_and_calendar_history(
    tmp_path, fake_readings, hour, expected_offset, expected_fold
):
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    run_date = date(2026, 11, 1)
    earliest = run_date - timedelta(days=30)
    write_day(data_dir, earliest, published_record(earliest))
    result, _ = await prepare(
        data_dir,
        tmp_path / "run",
        now=datetime(2026, 11, 1, hour, 30, tzinfo=UTC),
        run_date=run_date,
    )
    assert result.generated_at.hour == 1
    assert result.generated_at.fold == expected_fold
    assert result.generated_at.isoformat().endswith(expected_offset)
    context = json.loads((result.workspace / "context.json").read_text())
    assert context["date"] == "2026-11-01"
    assert context["now"].endswith(expected_offset)
    assert context["history"]["window_start"] == "2026-10-02"
    assert context["history"]["included_dates"] == ["2026-10-02"]


async def test_recent_tracks_count_against_catalog_preflight(tmp_path, fake_readings):
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    payload = published_record(RUN_DATE - timedelta(days=1))
    for index, track in enumerate(payload["tracks"][:5]):
        track["spotify_id"] = f"{index:022d}"
        track["spotify_url"] = f"https://open.spotify.com/track/{index:022d}"
    write_day(data_dir, RUN_DATE - timedelta(days=1), payload)
    with pytest.raises(ValueError, match="five eligible"):
        await prepare(data_dir, tmp_path / "run")


async def test_placeholder_persona_fails(tmp_path, fake_readings):
    write_sources(tmp_path / "data")
    (tmp_path / "data/personas.json").write_text(
        json.dumps(
            [
                {"id": "only", "name": "[Placeholder]", "bio": "TBD"},
            ]
        )
    )
    with pytest.raises(ValueError, match="real name"):
        await prepare(tmp_path / "data", tmp_path / "run")


async def test_mood_failure_propagates(tmp_path, fake_readings):
    write_sources(tmp_path / "data")
    mood_service = SimpleNamespace(score_mood=AsyncMock(side_effect=RuntimeError("mood failed")))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="mood failed"):
            await preparation.prepare_run(
                tmp_path / "data", tmp_path / "run", RUN_DATE, NOW, client, mood_service
            )
    assert (tmp_path / "run/workspace/catalog.jsonl").exists()
    assert not (tmp_path / "run/workspace/context.json").exists()
    diagnostics = json.loads((tmp_path / "run/diagnostics.json").read_text())
    assert diagnostics["inputs"][1]["raw"] == {"forecast": "retained"}
    assert "mood_usage" not in diagnostics
