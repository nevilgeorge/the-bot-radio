"""Export current-user Spotify playlist metadata and track counts, without individual tracks.

Usage: uv run python scripts/export_spotify_playlists.py [--output playlists.json]
Uses SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN from .env.
The refresh token selects the Spotify user; a client ID alone cannot identify a user.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx

from the_bot_radio.services.spotify_service import SpotifyError, SpotifyService
from the_bot_radio.web import config  # noqa: F401 (loads .env)


def playlist_metadata(
    summary: dict[str, Any], details: dict[str, Any] | None, error: str | None
) -> dict[str, Any]:
    """Keep high-level metadata and use Spotify's total, never the embedded page length."""
    metadata = {**summary, **(details or {})}
    track_count = None
    for payload in (details or {}, summary):
        for contents_key in ("items", "tracks"):
            contents = payload.get(contents_key)
            if isinstance(contents, dict) and type(contents.get("total")) is int:
                track_count = contents["total"]
                break
        if track_count is not None:
            break
    owner = metadata.get("owner") or {}
    external_urls = metadata.get("external_urls") or {}
    return {
        "id": metadata["id"],
        "name": metadata.get("name"),
        "description": metadata.get("description"),
        "spotify_url": external_urls.get("spotify"),
        "owner": {"id": owner.get("id"), "display_name": owner.get("display_name")},
        "public": metadata.get("public"),
        "collaborative": metadata.get("collaborative"),
        "track_count": track_count,
        "snapshot_id": metadata.get("snapshot_id"),
        "error": error,
    }


async def fetch_playlists(spotify_service: SpotifyService) -> list[dict[str, Any]]:
    """Fetch all summaries and retain partial results if an individual detail request fails."""
    summaries = await spotify_service.get_current_user_playlists()
    exported_playlists: list[dict[str, Any]] = []
    seen_playlist_ids: set[str] = set()
    for summary in summaries:
        playlist_id = summary["id"]
        if playlist_id in seen_playlist_ids:
            continue
        seen_playlist_ids.add(playlist_id)
        details: dict[str, Any] | None = None
        error_message: str | None = None
        try:
            details = await spotify_service.get_playlist_details(playlist_id)
        except SpotifyError as error:
            error_message = str(error)
        except httpx.HTTPError:
            error_message = "Spotify detail request failed due to a network error"
        exported_playlists.append(playlist_metadata(summary, details, error_message))
    return exported_playlists


async def run_export() -> list[dict[str, Any]]:
    """Use the existing authenticated Spotify client without making any remote changes."""
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        return await fetch_playlists(SpotifyService(http_client))


def main(argv: list[str] | None = None) -> int:
    """Write JSON to stdout or a requested file; signal incomplete exports with exit code one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    arguments = parser.parse_args(argv)
    try:
        playlists = anyio.run(run_export)
        output_json = json.dumps(playlists, indent=2, ensure_ascii=False) + "\n"
        if arguments.output is None:
            sys.stdout.write(output_json)
        else:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(output_json, encoding="utf-8")
    except (SpotifyError, RuntimeError, OSError, ValueError) as error:
        print(f"Playlist export failed: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Playlist export failed: could not reach Spotify", file=sys.stderr)
        return 1
    failure_count = sum(playlist["error"] is not None for playlist in playlists)
    print(f"Exported {len(playlists)} playlists; detail failures: {failure_count}", file=sys.stderr)
    return int(failure_count > 0)


if __name__ == "__main__":
    sys.exit(main())
