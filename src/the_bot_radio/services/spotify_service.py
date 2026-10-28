"""Every request The Bot Radio makes to the Spotify Web API, on a thin async httpx client.

Targets the post-February-2026 Development Mode API: playlists are created with
`POST /me/playlists`, playlist contents live under `/playlists/{id}/items` (`items.items[].item`
in payloads, formerly `tracks.tracks[].track`), and search returns at most 10 results per call.

Auth is the bot account's long-lived refresh token (from `scripts/spotify_auth.py`) exchanged
for short-lived access tokens. The token needs these scopes: `SCOPES`.
"""

import base64
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private ugc-image-upload"
SEARCH_LIMIT_MAX = 10  # Dev Mode cap (was 50)
ADD_ITEMS_BATCH = 100  # max URIs per add-items request
COVER_MAX_BYTES = 256_000  # max base64 payload for a custom cover
TOKEN_EXPIRY_MARGIN_SECONDS = 60
RETRY_AFTER_MAX_SECONDS = 30.0
JPEG_MAGIC = b"\xff\xd8\xff"


class SpotifyError(Exception):
    """A non-2xx response from Spotify, with the status and Spotify's error message."""

    def __init__(
        self, status_code: int, message: str, *, spotify_playlist_id: str | None = None
    ) -> None:
        super().__init__(f"Spotify {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.spotify_playlist_id = spotify_playlist_id


@dataclass(frozen=True)
class SpotifyCredentials:
    """The Spotify app's client id/secret plus the bot account's refresh token."""

    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls) -> "SpotifyCredentials":
        """Read `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` and `SPOTIFY_REFRESH_TOKEN`."""
        env_var_names = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN")
        missing_env_vars = [name for name in env_var_names if not os.environ.get(name)]
        if missing_env_vars:
            raise RuntimeError(
                f"missing Spotify credentials: {', '.join(missing_env_vars)} (see .env.example)"
            )
        return cls(*(os.environ[name] for name in env_var_names))


class SpotifyTrack(BaseModel):
    """A track, trimmed to the fields the bot uses."""

    id: str
    uri: str
    name: str
    artists: list[str]
    album: str
    duration_ms: int
    explicit: bool
    external_url: str | None = None


class SpotifyPlaylist(BaseModel):
    """A playlist on the bot account, with its tracks (empty if Spotify omitted them)."""

    id: str
    uri: str
    name: str
    description: str | None = None
    public: bool | None = None
    snapshot_id: str
    external_url: str | None = None
    tracks: list[SpotifyTrack] = []
    item_count: int | None = None


def build_track_query(title: str, artist: str) -> str:
    """Field-filtered search query for verifying a title/artist pair: `track:"…" artist:"…"`."""
    clean_title = title.replace('"', "")
    clean_artist = artist.replace('"', "")
    return f'track:"{clean_title}" artist:"{clean_artist}"'


def _parse_track(track_payload: dict[str, Any]) -> SpotifyTrack:
    """Normalize a Spotify track object."""
    return SpotifyTrack(
        id=track_payload["id"],
        uri=track_payload["uri"],
        name=track_payload["name"],
        artists=[artist["name"] for artist in track_payload.get("artists", [])],
        album=(track_payload.get("album") or {}).get("name", ""),
        duration_ms=track_payload.get("duration_ms", 0),
        explicit=track_payload.get("explicit", False),
        external_url=(track_payload.get("external_urls") or {}).get("spotify"),
    )


def _parse_playlist_items(item_rows: Sequence[dict[str, Any]]) -> list[SpotifyTrack]:
    """Tracks from playlist item rows, skipping removed (null), local and non-track items.

    Rows carry the track under `item` (Feb 2026); `track` is read as a fallback.
    """
    tracks: list[SpotifyTrack] = []
    for item_row in item_rows:
        track_payload = item_row.get("item") or item_row.get("track")
        if not track_payload or item_row.get("is_local") or track_payload.get("type") != "track":
            continue
        tracks.append(_parse_track(track_payload))
    return tracks


def _playlist_items_page(playlist_payload: dict[str, Any]) -> dict[str, Any]:
    """The playlist's first items page (`items`, or the pre-2026 `tracks`), or `{}`."""
    return playlist_payload.get("items") or playlist_payload.get("tracks") or {}


def _require_playlist_items_page(items_page: Any) -> dict[str, Any]:
    """Reject unavailable or malformed playlist contents during strict imports."""
    if items_page is None:
        raise SpotifyError(
            403,
            "playlist contents unavailable; the authenticated account must own or collaborate "
            "on the playlist (copy it into a bot-owned playlist first)",
        )
    if (
        not isinstance(items_page, dict)
        or not isinstance(items_page.get("items"), list)
        or (items_page.get("next") is not None and not isinstance(items_page["next"], str))
        or (
            "total" in items_page
            and (type(items_page["total"]) is not int or items_page["total"] < 0)
        )
        or any(
            not isinstance(item_row, dict)
            or not ("item" in item_row or "track" in item_row)
            or (
                item_row.get("item", item_row.get("track")) is not None
                and not isinstance(item_row.get("item", item_row.get("track")), dict)
            )
            for item_row in items_page.get("items", [])
        )
    ):
        raise SpotifyError(502, "playlist contents malformed or incomplete; import aborted")
    return items_page


def _parse_playlist(
    playlist_payload: dict[str, Any], tracks: list[SpotifyTrack] | None = None
) -> SpotifyPlaylist:
    """Normalize a Spotify playlist object; `tracks` overrides the embedded first page."""
    if tracks is None:
        tracks = _parse_playlist_items(_playlist_items_page(playlist_payload).get("items") or [])
    return SpotifyPlaylist(
        id=playlist_payload["id"],
        uri=playlist_payload["uri"],
        name=playlist_payload["name"],
        description=playlist_payload.get("description"),
        public=playlist_payload.get("public"),
        snapshot_id=playlist_payload["snapshot_id"],
        external_url=(playlist_payload.get("external_urls") or {}).get("spotify"),
        tracks=tracks,
    )


def _error_message(response: httpx.Response) -> str:
    """Spotify's `{"error": {"message"}}` (API) or `error_description` (accounts), else text."""
    try:
        error_body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    error_field = error_body.get("error") if isinstance(error_body, dict) else None
    if isinstance(error_field, dict):
        return error_field.get("message", str(error_field))
    if isinstance(error_body, dict) and "error_description" in error_body:
        return f"{error_field}: {error_body['error_description']}"
    return str(error_field or error_body)


class SpotifyService:
    """Wraps the Spotify Web API; one method per kind of request the bot makes."""

    def __init__(
        self, http_client: httpx.AsyncClient, credentials: SpotifyCredentials | None = None
    ) -> None:
        """Use the shared `http_client`; credentials default to the environment."""
        self.http_client = http_client
        self.credentials = credentials or SpotifyCredentials.from_env()
        self.access_token: str | None = None
        self.access_token_expires_at = 0.0  # time.monotonic() deadline

    async def refresh_token(self) -> str:
        """Exchange the refresh token for a new access token, cache it, and return it.

        If Spotify rotates the refresh token, the new one is used for the rest of this process
        and a warning says to update `SPOTIFY_REFRESH_TOKEN`.
        """
        response = await self.http_client.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self.credentials.refresh_token},
            auth=(self.credentials.client_id, self.credentials.client_secret),
        )
        if response.is_error:
            raise SpotifyError(response.status_code, _error_message(response))
        token_payload = response.json()
        self.access_token = token_payload["access_token"]
        self.access_token_expires_at = (
            time.monotonic() + token_payload.get("expires_in", 3600) - TOKEN_EXPIRY_MARGIN_SECONDS
        )
        rotated_refresh_token = token_payload.get("refresh_token")
        if rotated_refresh_token and rotated_refresh_token != self.credentials.refresh_token:
            log.warning("Spotify rotated the refresh token; update SPOTIFY_REFRESH_TOKEN")
            self.credentials = SpotifyCredentials(
                self.credentials.client_id, self.credentials.client_secret, rotated_refresh_token
            )
        return self.access_token

    async def _request(
        self, method: str, path_or_url: str, **request_kwargs: Any
    ) -> httpx.Response:
        """Send a request, retrying only explicit 401/429 rejections once each.

        Transport errors and cancellation propagate: a mutation may already have reached
        Spotify, so repeating it could create another playlist or duplicate its tracks.
        """
        url = path_or_url if path_or_url.startswith("https://") else f"{API_BASE}{path_or_url}"
        if self.access_token is None or time.monotonic() >= self.access_token_expires_at:
            await self.refresh_token()
        extra_headers: dict[str, str] = request_kwargs.pop("headers", {})
        refreshed_after_401 = waited_after_429 = False
        while True:
            headers = {**extra_headers, "Authorization": f"Bearer {self.access_token}"}
            response = await self.http_client.request(
                method, url, headers=headers, **request_kwargs
            )
            if response.status_code == 401 and not refreshed_after_401:
                refreshed_after_401 = True
                await self.refresh_token()
                continue
            if response.status_code == 429 and not waited_after_429:
                waited_after_429 = True
                retry_after_seconds = float(response.headers.get("Retry-After", "1"))
                await anyio.sleep(min(retry_after_seconds, RETRY_AFTER_MAX_SECONDS))
                continue
            if response.is_error:
                raise SpotifyError(response.status_code, _error_message(response))
            return response

    async def search_tracks(
        self, query: str, limit: int = SEARCH_LIMIT_MAX, offset: int = 0
    ) -> list[SpotifyTrack]:
        """Search the catalog for tracks; `limit` is clamped to the Dev Mode max of 10."""
        clamped_limit = max(1, min(limit, SEARCH_LIMIT_MAX))
        response = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": clamped_limit, "offset": offset},
        )
        track_payloads = response.json().get("tracks", {}).get("items") or []
        return [_parse_track(track_payload) for track_payload in track_payloads if track_payload]

    async def create_playlist(
        self, name: str, description: str, public: bool = True
    ) -> SpotifyPlaylist:
        """Create an empty playlist on the bot account."""
        response = await self._request(
            "POST",
            "/me/playlists",
            json={"name": name, "description": description, "public": public},
        )
        playlist_payload: Any = None
        try:
            playlist_payload = response.json()
            return _parse_playlist(playlist_payload, tracks=[])
        except (KeyError, TypeError, ValueError) as error:
            returned_id = playlist_payload.get("id") if isinstance(playlist_payload, dict) else None
            known_playlist_id = (
                returned_id if isinstance(returned_id, str) and returned_id.strip() else None
            )
            raise SpotifyError(
                502,
                "create-playlist returned a malformed acknowledgement",
                spotify_playlist_id=known_playlist_id,
            ) from error

    async def set_playlist_visibility(self, playlist_id: str, public: bool) -> None:
        """Change a bot-owned playlist's visibility; callers verify the result by reading it."""
        await self._request("PUT", f"/playlists/{playlist_id}", json={"public": public})

    async def get_current_user_playlists(self) -> list[dict[str, Any]]:
        """Return every playlist visible to the authorized user, following all list pages.

        Keep Spotify's summary fields intact for offline exports. The refresh token, not
        the client ID alone, identifies the current user.
        """
        playlists: list[dict[str, Any]] = []
        next_page_url: str | None = "/me/playlists"
        visited_pages: set[str] = set()
        while next_page_url:
            if next_page_url in visited_pages:
                raise SpotifyError(502, "playlist listing returned a repeated page")
            visited_pages.add(next_page_url)
            request_params = (
                {"limit": 50, "offset": 0} if next_page_url == "/me/playlists" else None
            )
            response = await self._request("GET", next_page_url, params=request_params)
            page = response.json()
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise SpotifyError(502, "playlist listing returned malformed contents")
            for playlist in page["items"]:
                if playlist is None:
                    continue
                if not isinstance(playlist, dict) or not isinstance(playlist.get("id"), str):
                    raise SpotifyError(502, "playlist listing returned an invalid playlist")
                playlists.append(playlist)
            next_page_url = page.get("next")
            if next_page_url is not None and not isinstance(next_page_url, str):
                raise SpotifyError(502, "playlist listing returned an invalid next page")
        return playlists

    async def get_playlist_details(self, playlist_id: str) -> dict[str, Any]:
        """Return Spotify's untrimmed Get Playlist response, including its first items page.

        Preserve owner, images, collaboration flags and any omitted contents exactly as
        returned. Use get_playlist() when all normalized tracks are needed instead.
        """
        payload = (await self._request("GET", f"/playlists/{playlist_id}")).json()
        if not isinstance(payload, dict):
            raise SpotifyError(502, "playlist details returned a malformed response")
        return payload

    async def get_playlist(
        self, playlist_id: str, *, require_contents: bool = False
    ) -> SpotifyPlaylist:
        """Fetch all tracks; optionally reject unavailable or malformed contents on import."""
        playlist_payload = await self.get_playlist_details(playlist_id)
        if require_contents:
            items_page = _require_playlist_items_page(
                playlist_payload.get("items", playlist_payload.get("tracks"))
            )
        else:
            items_page = _playlist_items_page(playlist_payload)
        tracks = _parse_playlist_items(items_page.get("items") or [])
        item_count = len(items_page.get("items") or [])
        expected_total = items_page.get("total")
        next_page_url = items_page.get("next")
        visited_pages: set[str] = set()
        while next_page_url:
            if not isinstance(next_page_url, str) or next_page_url in visited_pages:
                raise SpotifyError(502, "playlist contents returned an invalid or repeated page")
            visited_pages.add(next_page_url)
            items_page = (await self._request("GET", next_page_url)).json()
            if require_contents:
                items_page = _require_playlist_items_page(items_page)
                if items_page.get("total", expected_total) != expected_total:
                    raise SpotifyError(502, "playlist contents changed while reading pages")
            tracks.extend(_parse_playlist_items(items_page.get("items") or []))
            item_count += len(items_page.get("items") or [])
            next_page_url = items_page.get("next")
        if require_contents and expected_total is not None and item_count != expected_total:
            raise SpotifyError(502, "playlist contents malformed or incomplete; total mismatch")
        playlist = _parse_playlist(playlist_payload, tracks=tracks)
        playlist.item_count = item_count
        return playlist

    async def add_items_to_playlist(
        self, playlist_id: str, track_uris: Sequence[str], position: int | None = None
    ) -> str:
        """Append (or insert at `position`) track URIs in batches of 100, keeping their order.

        Returns the playlist's `snapshot_id` after the last batch.
        """
        if not track_uris:
            raise ValueError("no track URIs to add")
        snapshot_id = ""
        for batch_start in range(0, len(track_uris), ADD_ITEMS_BATCH):
            request_body: dict[str, Any] = {
                "uris": list(track_uris[batch_start : batch_start + ADD_ITEMS_BATCH])
            }
            if position is not None:
                request_body["position"] = position + batch_start
            response = await self._request(
                "POST", f"/playlists/{playlist_id}/items", json=request_body
            )
            try:
                acknowledgement = response.json()
            except ValueError as error:
                raise SpotifyError(502, "add-items returned a malformed acknowledgement") from error
            if (
                not isinstance(acknowledgement, dict)
                or not isinstance(acknowledgement.get("snapshot_id"), str)
                or not acknowledgement["snapshot_id"].strip()
            ):
                raise SpotifyError(502, "add-items returned a malformed acknowledgement")
            snapshot_id = acknowledgement["snapshot_id"]
        return snapshot_id

    async def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> None:
        """Set a custom cover from JPEG bytes (base64 payload ≤ 256 KB).

        Spotify answers 202 and processes the image asynchronously.
        """
        if not jpeg_bytes.startswith(JPEG_MAGIC):
            raise ValueError("cover image must be a JPEG")
        encoded_cover = base64.b64encode(jpeg_bytes)
        if len(encoded_cover) > COVER_MAX_BYTES:
            raise ValueError(
                f"cover image is {len(encoded_cover)} bytes base64; max is {COVER_MAX_BYTES}"
            )
        await self._request(
            "PUT",
            f"/playlists/{playlist_id}/images",
            content=encoded_cover,
            headers={"Content-Type": "image/jpeg"},
        )
