"""Publish validated playlists through deterministic, checkpointed Spotify operations."""

from datetime import datetime

from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.runtime import RunRecorder, write_json_atomic
from the_bot_radio.services.inputs.base import TZ
from the_bot_radio.services.spotify_service import SpotifyPlaylist, SpotifyService


async def publish_playlist(
    record: DailyRecord,
    spotify_service: SpotifyService,
    recorder: RunRecorder,
) -> DailyRecord:
    """Create a public playlist once, verify it, and retain known identities on failure.

    Mutations are never retried here: an ambiguous response may have changed Spotify.
    Artifact recovery uploads evidence only and cannot repeat these operations.
    """
    track_ids = [track.spotify_id for track in record.tracks]
    publication: dict[str, object] = {
        "requested_name": record.title,
        "requested_description": record.description,
        "requested_public": True,
        "ordered_track_ids": track_ids,
        "progress": "validated",
    }

    def progress(stage: str, **metadata: object) -> None:
        """Persist local progress before requesting an artifact checkpoint."""
        publication.update(progress=stage, **metadata)
        write_json_atomic(recorder.local_run_dir / "publication.json", publication)
        recorder.update_diagnostics(publication=publication.copy(), **metadata)

    def verify(playlist: SpotifyPlaylist) -> None:
        """Retain readback evidence and require every requested publication property."""
        observed = {
            "id": playlist.id,
            "name": playlist.name,
            "public": playlist.public,
            "ordered_track_ids": [track.id for track in playlist.tracks],
            "item_count": playlist.item_count,
            "snapshot_id": playlist.snapshot_id,
        }
        mismatched_fields = [
            field_name
            for field_name, matches in (
                ("id", playlist.id == publication["spotify_playlist_id"]),
                ("name", playlist.name == record.title),
                ("public", playlist.public is True),
                ("ordered_track_ids", observed["ordered_track_ids"] == track_ids),
                ("item_count", playlist.item_count in (None, len(track_ids))),
                ("snapshot_id", bool(playlist.snapshot_id)),
            )
            if not matches
        ]
        readbacks.append(
            {
                "expected_public": True,
                "observed": observed,
                "mismatched_fields": mismatched_fields,
            }
        )
        progress("verifying_public", readbacks=readbacks)
        if mismatched_fields:
            raise ValueError(
                "Spotify playlist readback does not match the requested publication: "
                + ", ".join(mismatched_fields)
            )

    readbacks: list[dict[str, object]] = []

    try:
        progress("validated")
        await recorder.checkpoint()
        progress("creating")
        playlist = await spotify_service.create_playlist(
            record.title, record.description, public=True
        )
        playlist_url = f"https://open.spotify.com/playlist/{playlist.id}"
        progress(
            "created",
            spotify_playlist_id=playlist.id,
            spotify_playlist_url=playlist_url,
        )
        # A failed identity checkpoint must stop before the next Spotify mutation.
        await recorder.checkpoint()
        progress("adding_tracks")
        await spotify_service.add_items_to_playlist(
            playlist.id, [f"spotify:track:{track_id}" for track_id in track_ids], position=0
        )
        progress("verifying_public")
        verified = await spotify_service.get_playlist(playlist.id, require_contents=True)
        verify(verified)
        published_at = datetime.now(TZ)
        published = DailyRecord.model_validate(
            {
                **record.model_dump(),
                "publication_status": "published",
                "spotify_playlist_id": playlist.id,
                "spotify_playlist_url": playlist_url,
                "spotify_snapshot_id": verified.snapshot_id,
                "spotify_playlist_public": True,
                "published_at": published_at,
            }
        )
        progress(
            "published",
            spotify_snapshot_id=verified.snapshot_id,
            spotify_playlist_public=True,
            published_at=published_at,
        )
        return published
    except BaseException as error:
        # Keep the uncertain operation in progress; do not imply rollback or retryability.
        known_id = getattr(error, "spotify_playlist_id", None)
        if known_id and "spotify_playlist_id" not in publication:
            progress(
                "create_response_invalid",
                spotify_playlist_id=known_id,
                spotify_playlist_url=f"https://open.spotify.com/playlist/{known_id}",
            )
        if "spotify_playlist_id" in publication:
            error.add_note(
                f"Retained Spotify playlist: {publication['spotify_playlist_url']} "
                f"(id={publication['spotify_playlist_id']})"
            )
        raise
