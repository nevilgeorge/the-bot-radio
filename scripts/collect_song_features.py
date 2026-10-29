"""Import Spotify playlists and cache ReccoBeats audio features as JSONL.

Run with a playlist URL/ID or --all-playlists. Shared provider services supply metadata and
features; this offline collection step is independent of the daily generation pipeline.
"""

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import anyio
import httpx
from pydantic import BaseModel, Field, ValidationError

from the_bot_radio.catalog import (
    AudioFeatures,
    CatalogTrack,
    Descriptors,
    LookupState,
    load_catalog,
)
from the_bot_radio.resources import RESOURCE_DIR
from the_bot_radio.services.reccobeats_service import ReccobeatsError, ReccobeatsService
from the_bot_radio.services.spotify_service import SpotifyError, SpotifyService
from the_bot_radio.web import config

PLAYLISTS_FILENAME = "shared_catalog_playlists.json"
CATALOG_FILENAME = "shared_catalog.jsonl"
SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
AUDIO_FIELDS = (
    "tempo",
    "energy",
    "valence",
    "danceability",
    "acousticness",
    "instrumentalness",
    "liveness",
    "loudness",
    "speechiness",
)


class CollectionSummary(BaseModel):
    """Current-run outcomes, including incomplete imports and cached source lookups."""

    tracks: int = 0
    cached: int = 0
    failures: int = 0
    playlists_failed: int = 0
    counts: dict[str, int] = Field(default_factory=dict)


def normalize_playlist_id(value: str) -> str:
    """Accept a Spotify playlist ID, URI, or official HTTPS playlist URL."""
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        playlist_id = value.removeprefix("spotify:playlist:")
    elif value.startswith("https://"):
        parsed_url = urlparse(value)
        path_parts = parsed_url.path.strip("/").split("/")
        if path_parts and path_parts[0].startswith("intl-"):
            path_parts = path_parts[1:]
        if parsed_url.netloc != "open.spotify.com" or len(path_parts) != 2:
            raise ValueError("expected a Spotify playlist URL, URI, or 22-character ID")
        if path_parts[0] != "playlist":
            raise ValueError("Spotify URL must identify a playlist")
        playlist_id = path_parts[1]
    else:
        playlist_id = value
    if not SPOTIFY_ID_RE.fullmatch(playlist_id):
        raise ValueError("expected a Spotify playlist URL, URI, or 22-character ID")
    return playlist_id


def select_playlist_ids(playlist: str | None, all_playlists: bool, data_dir: Path) -> list[str]:
    """Validate all selected inputs before contacting any external service."""
    if bool(playlist) == all_playlists:
        raise ValueError("provide one playlist or --all-playlists")
    if playlist:
        return [normalize_playlist_id(playlist)]
    playlist_values = json.loads((data_dir / PLAYLISTS_FILENAME).read_text())
    if not isinstance(playlist_values, list) or any(
        not isinstance(value, str) for value in playlist_values
    ):
        raise ValueError(f"{PLAYLISTS_FILENAME} must be a JSON array of playlist URLs")
    for value in playlist_values:
        if not value.startswith("https://"):
            raise ValueError(f"{PLAYLISTS_FILENAME} entries must be Spotify playlist URLs")
    return list(dict.fromkeys(normalize_playlist_id(value) for value in playlist_values))


def save_catalog(path: Path, records: Mapping[str, CatalogTrack]) -> None:
    """Replace the JSONL snapshot atomically, keeping readers on a complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            os.fchmod(output_file.fileno(), file_mode)
            for track in records.values():
                output_file.write(track.model_dump_json() + "\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def catalog_lock(path: Path) -> Iterator[None]:
    """Hold a nonblocking process lock; leave the lock inode in place across runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another collector is using this catalog") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def interpret_audio_features(
    spotify_ids: list[str], payload: dict[str, Any], fetched: date
) -> dict[str, AudioFeatures | None | ReccobeatsError]:
    """Join feature rows by Spotify href and isolate malformed rows from valid coverage."""
    rows = payload["content"]
    results: dict[str, AudioFeatures | None | ReccobeatsError] = dict.fromkeys(spotify_ids)
    unidentified = False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("href"), str):
            unidentified = True
            continue
        try:
            parsed_url = urlparse(row["href"])
        except ValueError:
            unidentified = True
            continue
        path_parts = parsed_url.path.strip("/").split("/")
        if parsed_url.netloc != "open.spotify.com" or len(path_parts) != 2:
            unidentified = True
            continue
        spotify_id = path_parts[1]
        if path_parts[0] != "track" or spotify_id not in results:
            unidentified = True
            continue
        try:
            results[spotify_id] = AudioFeatures(
                fetched=fetched, **{field: row[field] for field in AUDIO_FIELDS}
            )
        except (KeyError, ValidationError):
            results[spotify_id] = ReccobeatsError("reccobeats returned invalid audio features")
    if unidentified:
        for spotify_id, features in results.items():
            if features is None:
                results[spotify_id] = ReccobeatsError("reccobeats returned unidentified records")
    return results


def needs_lookup(track: CatalogTrack, source: str, refresh: bool) -> bool:
    """Successful and definitive empty results are cached; failed requests remain retryable."""
    state = track.lookups.get(source)
    return refresh or state is None or state.status == "error"


def record_result(
    track: CatalogTrack,
    source: Literal["reccobeats"],
    result: AudioFeatures | None | ReccobeatsError,
    now: datetime,
    counts: Counter[str],
) -> None:
    """Record an attempt, retaining earlier descriptors if a refresh fails."""
    if isinstance(result, ReccobeatsError):
        status = "error"
        track.lookups[source] = LookupState(status=status, attempted_at=now, error=str(result))
    else:
        status = "no_data" if result is None else "ok"
        track.lookups[source] = LookupState(status=status, attempted_at=now)
        track.descriptors.audio = result
    counts[f"{source}.{status}"] += 1


async def collect_catalog(
    playlist_ids: list[str],
    catalog_dir: Path,
    spotify_service: SpotifyService,
    reccobeats_service: ReccobeatsService,
    *,
    limit: int | None = None,
    refresh: bool = False,
    now: datetime | None = None,
) -> CollectionSummary:
    """Import metadata and enrich selected tracks with resumable audio-feature checkpoints."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    now = now or datetime.now(config.TZ)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(config.TZ)
    summary = CollectionSummary()
    counts: Counter[str] = Counter()
    catalog_path = catalog_dir / CATALOG_FILENAME
    with catalog_lock(catalog_path):
        records = load_catalog(catalog_path)
        selected_ids: dict[str, None] = {}
        for playlist_id in dict.fromkeys(playlist_ids):
            try:
                playlist = await spotify_service.get_playlist(playlist_id, require_contents=True)
            except (SpotifyError, httpx.HTTPError, ValueError, KeyError):
                print(
                    f"Playlist {playlist_id}: import failed; check access and Spotify credentials",
                    file=sys.stderr,
                )
                summary.playlists_failed += 1
                summary.failures += 1
                continue
            playlist_url = f"https://open.spotify.com/playlist/{playlist_id}"
            for spotify_track in playlist.tracks:
                if spotify_track.id not in selected_ids and (
                    limit is not None and len(selected_ids) >= limit
                ):
                    continue
                selected_ids[spotify_track.id] = None
                existing = records.get(spotify_track.id)
                track = CatalogTrack(
                    spotify_id=spotify_track.id,
                    title=spotify_track.name,
                    artists=spotify_track.artists,
                    album=spotify_track.album,
                    duration_ms=spotify_track.duration_ms,
                    spotify_url=f"https://open.spotify.com/track/{spotify_track.id}",
                    source_playlists=list(existing.source_playlists) if existing else [],
                    descriptors=existing.descriptors if existing else Descriptors(),
                    lookups=dict(existing.lookups) if existing else {},
                )
                if playlist_url not in track.source_playlists:
                    track.source_playlists.append(playlist_url)
                records[track.spotify_id] = track
        save_catalog(catalog_path, records)
        selected_tracks = [records[spotify_id] for spotify_id in selected_ids]
        summary.tracks = len(selected_tracks)
        counts["reccobeats.cached"] = sum(
            not needs_lookup(track, "reccobeats", refresh) for track in selected_tracks
        )
        summary.cached = counts["reccobeats.cached"]

        audio_tracks = [
            track for track in selected_tracks if needs_lookup(track, "reccobeats", refresh)
        ]
        for offset in range(0, len(audio_tracks), 20):
            batch = audio_tracks[offset : offset + 20]
            try:
                spotify_ids = [track.spotify_id for track in batch]
                payload = await reccobeats_service.get_audio_features(spotify_ids)
                results = interpret_audio_features(spotify_ids, payload, now.date())
            except ReccobeatsError as error:
                results = {track.spotify_id: error for track in batch}
            for track in batch:
                record_result(track, "reccobeats", results[track.spotify_id], now, counts)
            save_catalog(catalog_path, records)
            if any(
                isinstance(result, ReccobeatsError) and result.credential
                for result in results.values()
            ):
                break

        summary.failures += counts["reccobeats.error"]
        counts["reccobeats.available"] = sum(
            track.descriptors.audio is not None for track in selected_tracks
        )
        summary.counts = dict(counts)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse single-playlist and configured-playlist modes without contacting providers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist", nargs="?", help="Spotify playlist URL, URI or ID")
    parser.add_argument("--all-playlists", action="store_true", help=f"read {PLAYLISTS_FILENAME}")
    parser.add_argument("--limit", type=int, help="maximum unique tracks to collect")
    parser.add_argument("--refresh", action="store_true", help="refresh cached source results")
    parser.add_argument(
        "--data-dir", type=Path, default=config.DATA_DIR, help="directory containing playlist URLs"
    )
    parser.add_argument(
        "--catalog-dir", type=Path, default=RESOURCE_DIR, help="directory for the output catalog"
    )
    arguments = parser.parse_args(argv)
    if bool(arguments.playlist) == arguments.all_playlists:
        parser.error("provide one playlist or --all-playlists")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be positive")
    return arguments


async def run_collection(arguments: argparse.Namespace) -> CollectionSummary:
    """Build clients after validating the selected playlist inputs."""
    playlist_ids = select_playlist_ids(
        arguments.playlist, arguments.all_playlists, arguments.data_dir
    )
    if not playlist_ids:
        return CollectionSummary()
    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": "the-bot-radio/song-feature-collector"}
    ) as http_client:
        return await collect_catalog(
            playlist_ids,
            arguments.catalog_dir,
            SpotifyService(http_client),
            ReccobeatsService(http_client),
            limit=arguments.limit,
            refresh=arguments.refresh,
        )


def main() -> int:
    """Collect source data and print a compact coverage summary without exposing secrets."""
    arguments = parse_args()
    started_at = time.monotonic()
    try:
        summary = anyio.run(run_collection, arguments)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Collector stopped: {error}", file=sys.stderr)
        return 1
    print(
        f"Tracks: {summary.tracks}; cached lookups: {summary.cached}; "
        f"playlist failures: {summary.playlists_failed}; failures: {summary.failures}"
    )
    print(
        f"reccobeats: coverage={summary.counts.get('reccobeats.available', 0)}/{summary.tracks}; "
        f"cached={summary.cached}; new attempts: "
        + ", ".join(
            f"{status}={summary.counts.get(f'reccobeats.{status}', 0)}"
            for status in ("ok", "no_data", "error")
        )
    )
    print(f"Elapsed: {time.monotonic() - started_at:.1f}s")
    return int(summary.failures > 0)


if __name__ == "__main__":
    sys.exit(main())
