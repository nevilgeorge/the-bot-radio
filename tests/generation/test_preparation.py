"""Offline preparation coverage for frozen evidence and trusted run references."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from the_bot_radio.catalog import CatalogTrack
from the_bot_radio.generation import preparation
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.runtime import RunRecorder
from the_bot_radio.generation.storage import LocalRunArtifactStore
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
            data_dir, artifact_dir, run_date, now, client, mood_service, resources_dir=data_dir
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


class MemoryDailyStore:
    """Serve history bytes while recording concurrency and requested dates."""

    def __init__(self, records: dict[date, bytes]) -> None:
        """Keep independent source bytes for preparation's storage boundary."""
        self.records = records
        self.requested_dates: list[date] = []
        self.active_reads = 0
        self.peak_reads = 0

    def storage_location(self, run_date: date) -> str:
        """Compute the canonical fake URI without accessing stored bytes."""
        return f"memory://days/{run_date}.json"

    async def read(self, run_date: date) -> bytes | None:
        """Yield so simultaneous requests and out-of-order completions are observable."""
        self.requested_dates.append(run_date)
        self.active_reads += 1
        self.peak_reads = max(self.peak_reads, self.active_reads)
        try:
            await asyncio.sleep(0.001 * (run_date.day % 3))
            return self.records.get(run_date)
        finally:
            self.active_reads -= 1

    async def create(self, run_date: date, content: bytes) -> str:
        """Reject writes because history preparation is read-only."""
        raise AssertionError("preparation must not write daily records")


@pytest.mark.parametrize("history_days", [0, 7, 30])
async def test_injected_history_boundaries_and_coverage(history_days: int) -> None:
    """Empty, partial, and complete windows preserve chronological metadata."""
    included_dates = [RUN_DATE - timedelta(days=days) for days in range(history_days, 0, -1)]
    records = {
        playlist_date: json.dumps(published_record(playlist_date)).encode()
        for playlist_date in included_dates
    }
    records[RUN_DATE] = b"must not read today"
    records[RUN_DATE - timedelta(days=31)] = b"must not read before window"
    daily_store = MemoryDailyStore(records)
    snapshots, coverage = await preparation._load_history(daily_store, RUN_DATE)
    expected_dates = [RUN_DATE - timedelta(days=days) for days in range(30, 0, -1)]
    assert daily_store.requested_dates == expected_dates
    assert daily_store.peak_reads == 5
    assert daily_store.active_reads == 0
    assert [snapshot.record.date for snapshot in snapshots] == included_dates
    assert coverage == {
        "window_start": expected_dates[0].isoformat(),
        "window_end_exclusive": RUN_DATE.isoformat(),
        "included_dates": [playlist_date.isoformat() for playlist_date in included_dates],
        "missing_dates": [
            playlist_date.isoformat()
            for playlist_date in expected_dates
            if playlist_date not in included_dates
        ],
        "excluded_dates": [],
        "incomplete": history_days < 30,
    }


async def test_injected_history_bytes_and_diagnostics_callback(tmp_path, fake_readings):
    """Storage bytes become the snapshot unchanged and callback receives diagnostic updates."""
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    yesterday = RUN_DATE - timedelta(days=1)
    original_content = (" \n" + json.dumps(published_record(yesterday), indent=3) + " \n").encode()
    daily_store = MemoryDailyStore(
        {
            yesterday: original_content,
            RUN_DATE - timedelta(days=2): b'{"publication_status": "pending"}',
            RUN_DATE - timedelta(days=3): b'{"dj": "legacy"}',
        }
    )
    updates: list[dict[str, Any]] = []

    def diagnostics_sink(**values: Any) -> None:
        """Capture separate synchronous preparation updates."""
        updates.append(values)

    mood_service = SimpleNamespace(
        score_mood=AsyncMock(return_value=Mood(tempo_bpm=90, energy=4, brightness=3, obscurity=7)),
        last_mood_usage={"input_tokens": 20},
    )
    async with httpx.AsyncClient() as client:
        result = await preparation.prepare_run(
            data_dir,
            tmp_path / "run",
            RUN_DATE,
            NOW,
            client,
            mood_service,
            resources_dir=data_dir,
            daily_store=daily_store,
            diagnostics_sink=diagnostics_sink,
        )
    assert (
        result.workspace / f"history/{yesterday.isoformat()}.json"
    ).read_bytes() == original_content
    assert result.persona.id == "resident-1"
    assert result.context.recent_track_ids == frozenset(
        f"{100 + index:022d}" for index in range(10)
    )
    context = json.loads((result.workspace / "context.json").read_bytes())
    assert context["history"]["excluded_dates"] == ["2026-10-24", "2026-10-25"]
    assert len(updates) == 2
    assert updates[0]["inputs"][1]["raw"] == {"forecast": "retained"}
    assert updates[1] == {"mood_usage": {"input_tokens": 20}}
    assert not (result.artifact_dir / "diagnostics.json").exists()


@pytest.mark.parametrize(
    "content, message",
    [
        (b"not json", "Expecting value"),
        (b"[]", "invalid history record"),
        (b'{"publication_status": "published"}', "validation errors"),
        (
            json.dumps(published_record(RUN_DATE - timedelta(days=2))).encode(),
            "date does not match filename",
        ),
    ],
)
async def test_injected_invalid_history_fails(content: bytes, message: str) -> None:
    """Malformed payloads and invalid published records cannot silently shrink history."""
    daily_store = MemoryDailyStore({RUN_DATE - timedelta(days=1): content})
    with pytest.raises(ValueError, match=message):
        await preparation._load_history(daily_store, RUN_DATE)
    assert daily_store.active_reads == 0


@pytest.mark.parametrize("cancel_loading", [False, True])
async def test_history_failure_cancels_and_awaits_outstanding_reads(cancel_loading: bool) -> None:
    """Both storage failures and caller cancellation drain all in-flight reads."""
    reads_started = asyncio.Event()
    release_failure = asyncio.Event()
    active_dates: set[date] = set()
    cancelled_dates: set[date] = set()

    class BlockingDailyStore(MemoryDailyStore):
        """Hold five reads so failure cleanup is measured deterministically."""

        async def read(self, run_date: date) -> bytes | None:
            """Fail the first read or block until preparation cancels it."""
            active_dates.add(run_date)
            if len(active_dates) == 5:
                reads_started.set()
            try:
                if run_date == RUN_DATE - timedelta(days=30):
                    await release_failure.wait()
                    raise PermissionError("storage unavailable")
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_dates.add(run_date)
                raise
            finally:
                active_dates.remove(run_date)

    load_task = asyncio.create_task(preparation._load_history(BlockingDailyStore({}), RUN_DATE))
    await asyncio.wait_for(reads_started.wait(), timeout=1)
    if cancel_loading:
        load_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await load_task
    else:
        release_failure.set()
        with pytest.raises(PermissionError, match="storage unavailable"):
            await load_task
    assert not active_dates
    assert len(cancelled_dates) >= 4


async def test_history_payload_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filtering and validation share one decoded payload per downloaded day."""
    yesterday = RUN_DATE - timedelta(days=1)
    content = json.dumps(published_record(yesterday)).encode()
    original_loads = json.loads
    parsed_payloads: list[bytes] = []

    def counted_loads(payload: bytes) -> Any:
        """Track history decoding without altering its semantics."""
        parsed_payloads.append(payload)
        return original_loads(payload)

    monkeypatch.setattr(preparation.json, "loads", counted_loads)
    snapshots, _ = await preparation._load_history(MemoryDailyStore({yesterday: content}), RUN_DATE)
    assert parsed_payloads == [content]
    assert snapshots[0].content is content


async def test_preparation_diagnostics_work_with_run_recorder(tmp_path, fake_readings):
    """Preparation's synchronous callback integrates with persisted run checkpoints."""
    data_dir = tmp_path / "data"
    write_sources(data_dir)
    artifact_dir = tmp_path / "local-run"
    remote_dir = tmp_path / "stored-artifacts"
    recorder = RunRecorder(LocalRunArtifactStore(remote_dir), RUN_DATE, "integration", artifact_dir)
    mood_service = SimpleNamespace(
        score_mood=AsyncMock(return_value=Mood(tempo_bpm=90, energy=4, brightness=3, obscurity=7))
    )
    async with recorder:
        await recorder.start(purpose="preparation integration")
        async with httpx.AsyncClient() as client:
            result = await preparation.prepare_run(
                data_dir,
                artifact_dir,
                RUN_DATE,
                NOW,
                client,
                mood_service,
                resources_dir=data_dir,
                daily_store=MemoryDailyStore({}),
                diagnostics_sink=recorder.update_diagnostics,
            )
        await recorder.finish("prepared")
    stored_run = remote_dir / "runs/2026-10-27/integration"
    diagnostics = json.loads((stored_run / "diagnostics.json").read_bytes())
    assert diagnostics["status"] == "prepared"
    assert diagnostics["inputs"][1]["raw"] == {"forecast": "retained"}
    assert diagnostics["mood_usage"] == {}
    assert (stored_run / "workspace/context.json").read_bytes() == (
        result.workspace / "context.json"
    ).read_bytes()
