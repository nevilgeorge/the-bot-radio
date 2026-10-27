# Spotify — publishing playlists

Module: `src/the_bot_radio/services/spotify_service.py` (`SpotifyService`). A thin async client on
the shared `httpx.AsyncClient`; no SDK. Spotipy still calls the pre-2026 endpoints
(`/users/{id}/playlists`, `/playlists/{id}/tracks`), which Dev Mode apps can no longer use.

## Setup
1. Create an app in the Spotify developer dashboard (the app owner needs **Premium**; Dev Mode
   allows 5 users, which is fine since only the bot account logs in). Add the redirect URI
   `http://127.0.0.1:8888/callback` (Spotify rejects `localhost`; use the loopback IP).
2. Put `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` in `.env`.
3. Log in to open.spotify.com as the **bot account**, then run
   `uv run python scripts/spotify_auth.py` and paste the printed `SPOTIFY_REFRESH_TOKEN` into `.env`.

Scopes (`SCOPES`): `playlist-modify-public playlist-modify-private playlist-read-private
ugc-image-upload`. Re-run the script if scopes change.

## Endpoints
| Method | Call | Notes |
|---|---|---|
| `refresh_token()` | `POST accounts.spotify.com/api/token` | `grant_type=refresh_token`, Basic auth. Cached until `expires_in` − 60 s |
| `search_tracks()` | `GET /search?type=track` | `limit` clamped to **10** (Dev Mode max; default 5). Use `build_track_query(title, artist)` |
| `create_playlist()` | `POST /me/playlists` | Replaces `POST /users/{id}/playlists` |
| `get_current_user_playlists()` | `GET /me/playlists` | Follows all list pages, 50 playlists per request; preserves summary fields |
| `get_playlist_details()` | `GET /playlists/{id}` | Untrimmed response, including owner, images and the first embedded items page |
| `get_playlist()` | `GET /playlists/{id}` | Tracks under `items.items[].item` (was `tracks.items[].track`); follows `items.next` |
| `add_items_to_playlist()` | `POST /playlists/{id}/items` | ≤100 URIs per request; batched in order, `position` advanced per batch |
| `upload_playlist_cover()` | `PUT /playlists/{id}/images` | Body is base64 JPEG, `Content-Type: image/jpeg`, ≤256 KB encoded. Returns 202 (processed async) |

## Export the current user's playlists

```bash
uv run python scripts/export_spotify_playlists.py --output data/spotify_playlists.json
```

Omit `--output` to print JSON to stdout. The script lists every playlist returned by
`GET /me/playlists`, then calls `GET /playlists/{id}` for each unique ID. Each exported entry
contains the playlist ID, name, description, Spotify URL, owner ID/display name, public and
collaborative flags, track count, snapshot ID, and a nullable `error`. Individual tracks are
excluded. A detail failure preserves the available list metadata and does not stop later
playlists; incomplete exports return a nonzero exit code.

The current user is the account that authorized `SPOTIFY_REFRESH_TOKEN` for your app, not an
account determined by `SPOTIFY_CLIENT_ID` alone. The existing `playlist-read-private` scope
allows private playlists to be listed. If it was not granted, repeat the OAuth setup above.

Track counts use Spotify's reported total, falling back to the playlist-list response if
details omit it. Counts are null when unavailable; they are never inferred from the first
track page. The export does not fetch subsequent track pages. Dev Mode may omit contents for
followed playlists the account does not own or collaborate on; their metadata is still retained.

References: [Get Current User's Playlists](https://developer.spotify.com/documentation/web-api/reference/get-a-list-of-current-users-playlists),
[Get Playlist](https://developer.spotify.com/documentation/web-api/reference/get-playlist).

## Quirks
- 401: refreshed once and retried. 429: waits `Retry-After` (capped at 30 s) and retries once.
  Anything else non-2xx raises `SpotifyError(status_code, message)`.
- Spotify may rotate the refresh token in a refresh response. The new one is used for the rest
  of the process and a warning is logged; update `SPOTIFY_REFRESH_TOKEN` when you see it.
- `items` is omitted for playlists the bot doesn't own or collaborate on → `tracks == []` by
  default. Importers use `get_playlist(id, require_contents=True)` to raise `SpotifyError`
  instead, so inaccessible contents cannot be mistaken for an empty playlist. Copy found
  playlists into a bot-owned playlist before importing. Strict imports also reject malformed
  contents on any page; an explicitly empty `items` list is valid.
- Removed fields (Feb 2026): track `popularity`, `available_markets`, `linked_from`; user
  `country`/`email`/`product`. Don't depend on them.
- Forum reports of 403 on playlist create in Dev Mode: not an issue for our app; the live
  round trip (create, add, cover, read back) passed on 2026-10-26.

## Tests
`tests/services/test_spotify_service.py` (respx). Fixtures in `tests/services/fixtures/spotify/`
are trimmed real responses, captured by the live round-trip test:

```
RUN_LIVE=1 SPOTIFY_CAPTURE_FIXTURES=1 uv run pytest -m live -s tests/services/test_spotify_service.py
```

It creates a private "The Bot Radio — test" playlist on the bot account (delete it afterwards).
`cover.jpg` is a small JPEG used for the cover upload.
