"""Offline publication ordering, verification, and uncertain failure boundaries."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_models import daily_record_payload

from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.publication import publish_playlist
from the_bot_radio.generation.runtime import RunRecorder, recover_run_artifacts
from the_bot_radio.generation.storage import LocalRunArtifactStore
from the_bot_radio.services.spotify_service import SpotifyPlaylist, SpotifyTrack

pytestmark = pytest.mark.anyio


@pytest.fixture
def publication_services(tmp_path: Path) -> SimpleNamespace:
    """Provide a validated pending record and stateful Spotify responses."""
    record = DailyRecord.model_validate(daily_record_payload())
    record.title = f"{record.date} — Morning light"
    playlist = SpotifyPlaylist(
        id="p" * 22,
        uri=f"spotify:playlist:{'p' * 22}",
        name=record.title,
        public=True,
        snapshot_id="final-snapshot",
        tracks=[
            SpotifyTrack(
                id=track.spotify_id,
                uri=f"spotify:track:{track.spotify_id}",
                name=track.title,
                artists=track.artists,
                album="Album",
                duration_ms=track.duration_ms,
                explicit=False,
            )
            for track in record.tracks
        ],
    )
    events: list[str] = []

    async def create(*args: object, **kwargs: object) -> SpotifyPlaylist:
        """Return an identity before any tracks are inserted."""
        events.append("create")
        return playlist.model_copy(update={"tracks": []})

    async def add(*args: object, **kwargs: object) -> str:
        """Observe the identity checkpoint before changing contents."""
        identity = tmp_path / "archive" / "runs" / str(record.date) / "run" / "publication.json"
        assert json.loads(identity.read_bytes())["spotify_playlist_id"] == args[0]
        events.append("add")
        return "add-snapshot"

    async def read(*args: object, **kwargs: object) -> SpotifyPlaylist:
        """Read the current visibility and ordered tracks."""
        events.append("read")
        return playlist.model_copy(deep=True)

    spotify = SimpleNamespace(
        create_playlist=AsyncMock(side_effect=create),
        add_items_to_playlist=AsyncMock(side_effect=add),
        get_playlist=AsyncMock(side_effect=read),
    )
    recorder = RunRecorder(
        LocalRunArtifactStore(tmp_path / "archive"), record.date, "run", tmp_path / "staging"
    )
    return SimpleNamespace(
        record=record, playlist=playlist, spotify=spotify, recorder=recorder, events=events
    )


async def test_publication_order_and_final_metadata(publication_services) -> None:
    services = publication_services
    recorder = services.recorder
    await recorder.start()
    try:
        published = await publish_playlist(services.record, services.spotify, recorder)
        await recorder.finish("accepted")
    finally:
        await recorder.close()
    services.spotify.create_playlist.assert_awaited_once_with(
        services.record.title, services.record.description, public=True
    )
    services.spotify.add_items_to_playlist.assert_awaited_once_with(
        services.playlist.id,
        [f"spotify:track:{track.spotify_id}" for track in services.record.tracks],
        position=0,
    )
    assert services.events == ["create", "add", "read"]
    assert published.publication_status == "published"
    assert published.spotify_snapshot_id == "final-snapshot"
    assert published.spotify_playlist_public is True
    assert published.published_at.utcoffset() is not None
    assert published.spotify_playlist_url.endswith(services.playlist.id)
    publication = json.loads((recorder.local_run_dir / "publication.json").read_bytes())
    assert [readback["expected_public"] for readback in publication["readbacks"]] == [True]
    assert all(not readback["mismatched_fields"] for readback in publication["readbacks"])


@pytest.mark.parametrize(
    "method",
    ["create_playlist", "add_items_to_playlist", "get_playlist"],
)
@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_is_not_retried_and_recovery_only_uploads(
    publication_services, method: str, cancel: bool, tmp_path: Path
) -> None:
    services = publication_services
    error = asyncio.CancelledError() if cancel else OSError("lost response")
    getattr(services.spotify, method).side_effect = error
    await services.recorder.start()
    try:
        with pytest.raises(type(error)) as captured:
            await publish_playlist(services.record, services.spotify, services.recorder)
        assert getattr(services.spotify, method).await_count == 1
        progress = json.loads((services.recorder.local_run_dir / "publication.json").read_bytes())
        if method != "create_playlist":
            assert progress["spotify_playlist_id"] == services.playlist.id
            assert "Retained Spotify playlist:" in captured.value.__notes__[0]
        await services.recorder.finish("failed")
    finally:
        await services.recorder.close()
    call_counts = [mock.await_count for mock in vars(services.spotify).values()]
    await recover_run_artifacts(
        LocalRunArtifactStore(tmp_path / "recovered"),
        services.record.date,
        "run",
        services.recorder.local_run_dir,
    )
    assert call_counts == [mock.await_count for mock in vars(services.spotify).values()]
    assert list((tmp_path / "recovered").rglob("publication.json"))


@pytest.mark.parametrize(
    ("mismatch", "expected_field"),
    [
        ("order", "ordered_track_ids"),
        ("missing", "ordered_track_ids"),
        ("extra", "ordered_track_ids"),
        ("name", "name"),
        ("visibility", "public"),
        ("unknown_visibility", "public"),
        ("identity", "id"),
        ("item_count", "item_count"),
        ("snapshot", "snapshot_id"),
    ],
)
async def test_readback_mismatch_prevents_publication(
    publication_services: SimpleNamespace, mismatch: str, expected_field: str
) -> None:
    """The failed check and exact observed response survive locally and in the archive."""
    services = publication_services
    if mismatch == "order":
        services.playlist.tracks.reverse()
    elif mismatch == "missing":
        services.playlist.tracks.pop()
    elif mismatch == "extra":
        services.playlist.tracks.append(services.playlist.tracks[0])
    elif mismatch == "name":
        services.playlist.name = "Unexpected name"
    elif mismatch == "visibility":
        services.playlist.public = False
    elif mismatch == "unknown_visibility":
        services.playlist.public = None
    elif mismatch == "item_count":
        services.playlist.item_count = 11
    elif mismatch == "snapshot":
        services.playlist.snapshot_id = ""
    elif mismatch == "identity":
        original_create = services.spotify.create_playlist.side_effect

        async def create(*args: object, **kwargs: object) -> SpotifyPlaylist:
            """Return the correct create ID followed by an inconsistent readback."""
            created = await original_create(*args, **kwargs)
            services.playlist.id = "q" * 22
            return created

        services.spotify.create_playlist.side_effect = create
    await services.recorder.start()
    try:
        with pytest.raises(ValueError, match=f"readback.*: {expected_field}"):
            await publish_playlist(services.record, services.spotify, services.recorder)
        await services.recorder.finish("failed")
        publication = json.loads(
            (services.recorder.local_run_dir / "publication.json").read_bytes()
        )
        [readback] = publication["readbacks"]
        assert readback["expected_public"] is True
        assert readback["mismatched_fields"] == [expected_field]
        assert readback["observed"] == {
            "id": services.playlist.id,
            "name": services.playlist.name,
            "public": services.playlist.public,
            "ordered_track_ids": [track.id for track in services.playlist.tracks],
            "item_count": services.playlist.item_count,
            "snapshot_id": services.playlist.snapshot_id,
        }
        archived_run = (
            services.recorder.artifact_store.data_dir / "runs" / str(services.record.date) / "run"
        )
        assert json.loads((archived_run / "publication.json").read_bytes()) == publication
        archived_diagnostics = json.loads((archived_run / "diagnostics.json").read_bytes())
        assert archived_diagnostics["publication"] == publication
    finally:
        await services.recorder.close()


@pytest.mark.parametrize("checkpoint_number", [1, 2])
@pytest.mark.parametrize("cancel", [False, True])
async def test_checkpoint_failure_stops_before_next_mutation(
    publication_services, checkpoint_number: int, cancel: bool
) -> None:
    """Evidence and identity must be archived before starting another Spotify mutation."""
    services = publication_services
    recorder = services.recorder
    await recorder.start()
    original_checkpoint = recorder.checkpoint
    attempts = 0

    async def checkpoint() -> None:
        """Fail one explicit checkpoint without changing artifact retry policy."""
        nonlocal attempts
        attempts += 1
        if attempts == checkpoint_number:
            if cancel:
                raise asyncio.CancelledError()
            raise OSError("checkpoint unavailable")
        await original_checkpoint()

    recorder.checkpoint = checkpoint
    try:
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await publish_playlist(services.record, services.spotify, recorder)
        assert services.spotify.create_playlist.await_count == checkpoint_number - 1
        services.spotify.add_items_to_playlist.assert_not_awaited()
        progress = json.loads((recorder.local_run_dir / "publication.json").read_bytes())
        assert progress["progress"] == ("validated" if checkpoint_number == 1 else "created")
    finally:
        await recorder.close()
