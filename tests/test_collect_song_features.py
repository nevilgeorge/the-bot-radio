"""Offline coverage for playlist feature collection, persistence and resumption."""

import copy
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from the_bot_radio.services.spotify_service import SpotifyPlaylist, SpotifyTrack

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collect_song_features.py"
MODULE_SPEC = importlib.util.spec_from_file_location("song_feature_collector", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
collector = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = collector
MODULE_SPEC.loader.exec_module(collector)

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures" / "song_features"
TRACK_ID = "00aqkszH1FdUiJJWvX6iEl"
SECOND_TRACK_ID = "1" * 22
PLAYLIST_ID = "2" * 22
SECOND_PLAYLIST_ID = "3" * 22
FETCH_DATE = date(2026, 10, 26)
FETCH_TIME = datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on the project's asyncio backend."""
    return "asyncio"


def fixture_payload(name: str) -> dict:
    """Load a fresh copy of a documented or captured provider response."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def catalog_track(spotify_id: str = TRACK_ID, title: str = "Wicked Games"):
    """Build a minimal catalog record for the captured audio-feature track."""
    return collector.CatalogTrack(
        spotify_id=spotify_id,
        title=title,
        artists=["The Weeknd"],
        album="House of Balloons",
        duration_ms=240000,
        spotify_url=f"https://open.spotify.com/track/{spotify_id}",
    )


def spotify_playlist(playlist_id: str = PLAYLIST_ID, track_ids: list[str] | None = None):
    """Construct a normalized playlist without exercising Spotify HTTP transport."""
    return SpotifyPlaylist(
        id=playlist_id,
        uri=f"spotify:playlist:{playlist_id}",
        name="Shared catalog source",
        snapshot_id="snapshot",
        tracks=[
            SpotifyTrack(
                id=track_id,
                uri=f"spotify:track:{track_id}",
                name="Wicked Games",
                artists=["The Weeknd"],
                album="House of Balloons",
                duration_ms=240000,
                explicit=False,
            )
            for track_id in (track_ids if track_ids is not None else [TRACK_ID])
        ],
    )


@pytest.mark.parametrize(
    "playlist_reference",
    [
        PLAYLIST_ID,
        f"spotify:playlist:{PLAYLIST_ID}",
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=share",
    ],
)
def test_normalize_playlist_references(playlist_reference: str) -> None:
    assert collector.normalize_playlist_id(playlist_reference) == PLAYLIST_ID


@pytest.mark.parametrize("reference", ["", "short", "https://evil.example/playlist/" + PLAYLIST_ID])
def test_reject_invalid_playlist_references(reference: str) -> None:
    with pytest.raises(ValueError):
        collector.normalize_playlist_id(reference)


def test_playlist_file_deduplicates_and_validates_every_entry(tmp_path: Path) -> None:
    playlist_path = tmp_path / "shared_catalog_playlists.json"
    playlist_path.write_text(
        json.dumps(
            [
                f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
                f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=another",
                f"https://open.spotify.com/playlist/{SECOND_PLAYLIST_ID}",
            ]
        )
    )
    assert collector.select_playlist_ids(None, True, tmp_path) == [PLAYLIST_ID, SECOND_PLAYLIST_ID]
    playlist_path.write_text(json.dumps([f"https://open.spotify.com/playlist/{PLAYLIST_ID}", 7]))
    with pytest.raises(ValueError):
        collector.select_playlist_ids(None, True, tmp_path)


def test_cli_keeps_catalog_in_package_when_playlist_directory_changes(tmp_path: Path) -> None:
    """Playlist configuration and the shipped catalog have independent locations."""
    arguments = collector.parse_args(["--all-playlists", "--data-dir", str(tmp_path)])
    assert arguments.data_dir == tmp_path
    assert arguments.catalog_dir == collector.RESOURCE_DIR
    catalog_directory = tmp_path / "catalog"
    arguments = collector.parse_args([PLAYLIST_ID, "--catalog-dir", str(catalog_directory)])
    assert arguments.catalog_dir == catalog_directory


def test_catalog_round_trip_and_invalid_records(tmp_path: Path) -> None:
    catalog_path = tmp_path / "shared_catalog.jsonl"
    original_track = catalog_track()
    collector.save_catalog(catalog_path, {TRACK_ID: original_track})
    assert collector.load_catalog(catalog_path)[TRACK_ID] == original_track
    original_content = catalog_path.read_text()
    catalog_path.write_text(original_content + original_content)
    with pytest.raises(ValueError):
        collector.load_catalog(catalog_path)
    catalog_path.write_text('{"broken":')
    with pytest.raises(ValueError):
        collector.load_catalog(catalog_path)
    assert catalog_path.read_text() == '{"broken":'


def test_catalog_permissions_allow_server_reads_and_preserve_existing_mode(tmp_path: Path) -> None:
    catalog_path = tmp_path / "shared_catalog.jsonl"
    records = {TRACK_ID: catalog_track()}
    collector.save_catalog(catalog_path, records)
    assert catalog_path.stat().st_mode & 0o777 == 0o644
    catalog_path.chmod(0o640)
    collector.save_catalog(catalog_path, records)
    assert catalog_path.stat().st_mode & 0o777 == 0o640


def test_catalog_lock_rejects_concurrent_writer_and_releases(tmp_path: Path) -> None:
    catalog_path = tmp_path / "shared_catalog.jsonl"
    with collector.catalog_lock(catalog_path):
        with pytest.raises(ValueError, match="another collector"):
            with collector.catalog_lock(catalog_path):
                pytest.fail("second writer acquired the catalog lock")
    with collector.catalog_lock(catalog_path):
        pass


@respx.mock
async def test_reccobeats_matches_spotify_href_and_handles_missing_records() -> None:
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json=fixture_payload("reccobeats"))
    )
    async with httpx.AsyncClient() as http_client:
        reccobeats_client = collector.ReccoBeatsClient(http_client, request_interval=0)
        results = await reccobeats_client.fetch_features([SECOND_TRACK_ID, TRACK_ID], FETCH_DATE)
    assert results[SECOND_TRACK_ID] is None
    assert results[TRACK_ID].energy == 0.57
    assert results[TRACK_ID].tempo == 114.033
    assert results[TRACK_ID].fetched == FETCH_DATE
    assert TRACK_ID in audio_route.calls.last.request.url.params["ids"]


@respx.mock
async def test_reccobeats_bad_record_does_not_discard_good_record() -> None:
    payload = fixture_payload("reccobeats")
    malformed_track = copy.deepcopy(payload["content"][0])
    malformed_track["href"] = f"https://open.spotify.com/track/{SECOND_TRACK_ID}"
    malformed_track["energy"] = "invalid"
    payload["content"].append(malformed_track)
    payload["content"][0]["energy"] = 0
    respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with httpx.AsyncClient() as http_client:
        results = await collector.ReccoBeatsClient(http_client, request_interval=0).fetch_features(
            [TRACK_ID, SECOND_TRACK_ID], FETCH_DATE
        )
    assert results[TRACK_ID].energy == 0
    assert isinstance(results[SECOND_TRACK_ID], collector.ProviderError)


@respx.mock
async def test_retry_after_survives_exhaustion_before_next_batch(monkeypatch) -> None:
    fake_sleep = AsyncMock()
    monkeypatch.setattr(collector.anyio, "sleep", fake_sleep)
    monkeypatch.setattr(collector, "time", SimpleNamespace(monotonic=lambda: 100.0))
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, json=fixture_payload("reccobeats")),
        ]
    )
    async with httpx.AsyncClient() as http_client:
        reccobeats_client = collector.ReccoBeatsClient(http_client)
        with pytest.raises(collector.ProviderError, match="HTTP 429"):
            await reccobeats_client.fetch_features([TRACK_ID], FETCH_DATE)
        features = await reccobeats_client.fetch_features([TRACK_ID], FETCH_DATE)
    assert features[TRACK_ID].energy == 0.57 and audio_route.call_count == 4
    assert fake_sleep.await_args_list[-1].args == (120.0,)


@respx.mock
async def test_collection_reuses_cross_playlist_cache_and_merges_provenance(tmp_path: Path) -> None:
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json=fixture_payload("reccobeats"))
    )
    spotify_service = AsyncMock()
    spotify_service.get_playlist.return_value = spotify_playlist(track_ids=[TRACK_ID, TRACK_ID])
    async with httpx.AsyncClient() as http_client:
        reccobeats_client = collector.ReccoBeatsClient(http_client, request_interval=0)
        await collector.collect_catalog(
            [PLAYLIST_ID], tmp_path, spotify_service, reccobeats_client, now=FETCH_TIME
        )
        spotify_service.get_playlist.return_value = spotify_playlist(SECOND_PLAYLIST_ID)
        summary = await collector.collect_catalog(
            [SECOND_PLAYLIST_ID], tmp_path, spotify_service, reccobeats_client, now=FETCH_TIME
        )
        assert summary.failures == 0 and summary.cached == 1
        assert summary.counts["reccobeats.cached"] == 1
        assert summary.counts["reccobeats.available"] == 1
        assert audio_route.call_count == 1
        record = collector.load_catalog(tmp_path / "shared_catalog.jsonl")[TRACK_ID]
        assert set(record.source_playlists) == {
            f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
            f"https://open.spotify.com/playlist/{SECOND_PLAYLIST_ID}",
        }
        await collector.collect_catalog(
            [SECOND_PLAYLIST_ID],
            tmp_path,
            spotify_service,
            reccobeats_client,
            now=FETCH_TIME,
            refresh=True,
        )
    assert audio_route.call_count == 2


@respx.mock
async def test_collection_global_limit_and_failure_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(collector.anyio, "sleep", AsyncMock())
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(503)
    )
    spotify_service = AsyncMock()
    spotify_service.get_playlist.side_effect = [
        spotify_playlist(track_ids=[TRACK_ID]),
        spotify_playlist(SECOND_PLAYLIST_ID, [TRACK_ID, SECOND_TRACK_ID]),
    ]
    async with httpx.AsyncClient() as http_client:
        summary = await collector.collect_catalog(
            [PLAYLIST_ID, SECOND_PLAYLIST_ID],
            tmp_path,
            spotify_service,
            collector.ReccoBeatsClient(http_client, request_interval=0),
            limit=1,
            now=FETCH_TIME,
        )
    records = collector.load_catalog(tmp_path / "shared_catalog.jsonl")
    assert list(records) == [TRACK_ID]
    assert records[TRACK_ID].descriptors.audio is None
    assert records[TRACK_ID].lookups["reccobeats"].status == "error"
    assert summary.failures == 1 and audio_route.call_count == 3
    assert len(records[TRACK_ID].source_playlists) == 2


@respx.mock
async def test_failed_refresh_preserves_successful_descriptors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(collector.anyio, "sleep", AsyncMock())
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json=fixture_payload("reccobeats"))
    )
    spotify_service = AsyncMock()
    spotify_service.get_playlist.return_value = spotify_playlist()
    async with httpx.AsyncClient() as http_client:
        reccobeats_client = collector.ReccoBeatsClient(http_client, request_interval=0)
        await collector.collect_catalog(
            [PLAYLIST_ID], tmp_path, spotify_service, reccobeats_client, now=FETCH_TIME
        )
        before = collector.load_catalog(tmp_path / "shared_catalog.jsonl")[TRACK_ID]
        audio_route.mock(return_value=httpx.Response(503))
        summary = await collector.collect_catalog(
            [PLAYLIST_ID],
            tmp_path,
            spotify_service,
            reccobeats_client,
            now=FETCH_TIME,
            refresh=True,
        )
    after = collector.load_catalog(tmp_path / "shared_catalog.jsonl")[TRACK_ID]
    assert after.descriptors == before.descriptors
    assert summary.failures > 0


@respx.mock
async def test_interrupted_collection_resumes_after_audio_checkpoint(tmp_path: Path) -> None:
    track_ids = [TRACK_ID] + [str(index).zfill(22) for index in range(1, 21)]
    payload = fixture_payload("reccobeats")
    payload["content"] = [
        {**payload["content"][0], "href": f"https://open.spotify.com/track/{track_id}"}
        for track_id in track_ids
    ]
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json={"content": payload["content"][-1:]})
    )
    spotify_service = AsyncMock()
    spotify_service.get_playlist.return_value = spotify_playlist(track_ids=track_ids)
    interrupted_client = AsyncMock()
    first_batch = {
        track_id: collector.AudioFeatures(
            fetched=FETCH_DATE,
            **{field: payload["content"][0][field] for field in collector.AUDIO_FIELDS},
        )
        for track_id in track_ids[:20]
    }
    interrupted_client.fetch_features.side_effect = [
        first_batch,
        RuntimeError("interrupted before second batch"),
    ]
    with pytest.raises(RuntimeError, match="interrupted"):
        await collector.collect_catalog(
            [PLAYLIST_ID], tmp_path, spotify_service, interrupted_client, now=FETCH_TIME
        )
    checkpoint = collector.load_catalog(tmp_path / "shared_catalog.jsonl")
    assert checkpoint[track_ids[0]].descriptors.audio is not None
    assert checkpoint[track_ids[-1]].descriptors.audio is None
    async with httpx.AsyncClient() as http_client:
        summary = await collector.collect_catalog(
            [PLAYLIST_ID],
            tmp_path,
            spotify_service,
            collector.ReccoBeatsClient(http_client, request_interval=0),
            now=FETCH_TIME,
        )
    assert audio_route.call_count == 1 and summary.cached == 20
    assert audio_route.calls.last.request.url.params["ids"] == track_ids[-1]
    assert summary.counts["reccobeats.available"] == 21


@respx.mock
async def test_no_data_is_cached_without_operational_failure(tmp_path: Path) -> None:
    audio_route = respx.get("https://api.reccobeats.com/v1/audio-features").mock(
        return_value=httpx.Response(200, json={"content": []})
    )
    spotify_service = AsyncMock()
    spotify_service.get_playlist.return_value = spotify_playlist()
    async with httpx.AsyncClient() as http_client:
        reccobeats_client = collector.ReccoBeatsClient(http_client, request_interval=0)
        for _ in range(2):
            summary = await collector.collect_catalog(
                [PLAYLIST_ID], tmp_path, spotify_service, reccobeats_client, now=FETCH_TIME
            )
            assert summary.failures == 0
    track = collector.load_catalog(tmp_path / "shared_catalog.jsonl")[TRACK_ID]
    assert track.lookups["reccobeats"].status == "no_data"
    assert track.descriptors.audio is None
    assert audio_route.call_count == 1 and summary.cached == 1
    assert summary.counts["reccobeats.available"] == 0
    assert summary.counts["reccobeats.cached"] == 1
