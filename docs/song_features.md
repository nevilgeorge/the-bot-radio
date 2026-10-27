# Shared catalog feature collection

`scripts/collect_song_features.py` imports Spotify playlist tracks and enriches them with
ReccoBeats audio features. `ReccoBeatsClient` lives inside the script; Spotify requests use
the existing `SpotifyService`.
This is an offline curation tool, not part of the daily generation job. It does not call Claude
or assign tracks to residents.

## Setup and commands

Configure Spotify credentials using [spotify.md](spotify.md).
ReccoBeats requires no API key. Spotify's current Development Mode only exposes contents of
playlists the authenticated account owns or collaborates on. Copy a found playlist into an
accessible playlist before importing; following it is not sufficient.

Import one playlist using its URL, URI or ID:

```bash
uv run python scripts/collect_song_features.py PLAYLIST_URL_OR_ID
```

Or edit `data/shared_catalog_playlists.json` to contain an array of playlist URLs, then import
all of them:

```json
[
  "https://open.spotify.com/playlist/PLAYLIST_ID"
]
```

```bash
uv run python scripts/collect_song_features.py --all-playlists --limit 50
uv run python scripts/collect_song_features.py --all-playlists
```

The positional playlist and `--all-playlists` are mutually exclusive. `--limit N` selects the
first N unique tracks across playlists in input order. `--data-dir PATH` overrides
`BOT_RADIO_DATA_DIR` for both the playlist list and the output catalog. `--refresh` repeats
completed lookups. All playlist inputs are validated before network requests begin.

## Data and caching

The output is `data/shared_catalog.jsonl`: one JSON object per unique Spotify track ID. Each
record contains Spotify metadata, source playlist provenance, a nullable `descriptors.audio`
block, and ReccoBeats lookup state. The audio block carries its source and fetch date;
attempt timestamps are timezone-aware America/New_York values.

Generate the JSONL offline, then include it in the deployment's configured data directory.
The server only reads this internal file; it does not run the collector or make feature
requests. No HTTP route or static-file mount exposes the catalog. Regenerate
it offline and deploy the updated file when the source playlists change.

Re-importing merges playlist provenance without duplicating songs. Removing a song from a
source playlist does not remove its catalog record. Successful and empty lookups
are cached until `--refresh`; failed requests are retried on the next run. A failed refresh
preserves previously successful descriptors. Atomic checkpoints allow an interrupted run to
resume from completed lookups. Malformed existing JSONL or duplicate IDs abort before any
catalog overwrite. A process-held lock prevents simultaneous collectors.

The final summary reports unique tracks, cache hits, stored coverage, new-attempt outcomes,
operational failures and elapsed time. Missing coverage is normal and does not fail the command.
Playlist or provider request failures produce a nonzero exit after available progress has been saved.

## ReccoBeats details

Use `GET https://api.reccobeats.com/v1/audio-features?ids=...`, in batches of 20.
Match results to Spotify IDs through `href`; the response `id` is a ReccoBeats UUID and
response order is not a reliable join key. Retain tempo, energy, valence, danceability,
acousticness, instrumentalness, liveness, loudness and speechiness on their source scales.

ReccoBeats requests are paced to at most one request per second, with up to three attempts for
transient errors and support for `Retry-After`. Access failures stop further feature requests
for the remainder of the run, preserving imported metadata and completed results.

References: [ReccoBeats audio features](https://reccobeats.com/docs/apis/get-audio-features),
[ReccoBeats rate limits](https://reccobeats.com/docs/documentation/rate-limiting).
