## Commands

- Lint/format: `uv run ruff check . && uv run ruff format .` (line length 100; `docs/` is excluded)
- S3 qualification is skipped by default: `RUN_LIVE=1 uv run pytest -m s3_live -s`. Require an explicit bucket; retain all data beneath unique `qualification/<uuid>/` prefixes. No live providers or deletion.
- Live API tests are skipped by default. Run them with `RUN_LIVE=1 uv run pytest -m live -s`; `tests/services/test_runner.py::test_live_read_all` prints every input's reading for today.

## Conventions

- All dates/times are America/New_York (`web/config.py` `TZ`). Pass timezone-aware datetimes; in input services use `ctx.run_date` / `ctx.now`, never `date.today()` / `datetime.now()`.
- Async tests use anyio (not pytest-asyncio) and mock HTTP with respx. Each test module must set `pytestmark = pytest.mark.anyio` itself — a `pytestmark` in conftest.py does not apply.
- The site and generation pipeline share one JSON contract, with website schema in `src/the_bot_radio/web/models.py`. Local mode uses `data/days/YYYY-MM-DD.json`; S3 mode uses `<prefix>/days/YYYY-MM-DD.json`. Each generation invocation replaces its date’s daily record only after validation and archiving identical bytes at `runs/YYYY-MM-DD/<run-id>/YYYY-MM-DD.json`. The newest visible record is the Today page; local files hot reload, S3 snapshots refresh periodically, and invalid records are logged and skipped.
- Optional S3 consumers use the same bucket/prefix and SDK configuration as generation. Website lifespan owns one client and a memory cache refreshed 60 seconds after each attempt; startup fails on infrastructure errors, subsequent failures retain the entire snapshot. Store methods never perform remote I/O. Personas and the shared catalog ship in `src/the_bot_radio/resources/`; input descriptions remain in local `data/`. Tests must inject explicit local configuration or fake S3 dependencies, never ambient AWS settings.
- Weekday/date labels are always derived from dates (`web/view_helpers.py`), never hard-coded in templates or data.
- Page CSS lives in the single `static/css/site.css` (tokens on `:root`, dark only). Templates use Jinja macros from `templates/partials/`; derived values come from `view_helpers` (exposed to templates as `h`).

## LLM requests

- Standalone Anthropic requests, including mood scoring, live in `src/the_bot_radio/services/claude_service.py` (`ClaudeService`). Playlist Agent SDK orchestration and its system prompt live in `src/the_bot_radio/generation/agent.py` (`PlaylistAgent`). Credentials come from `ANTHROPIC_API_KEY` (or an `ant auth login` profile).
- Run one playlist agent task per generation invocation. The agent may validate and revise through tools during that task; invalid final output fails without a host repair prompt. The daily job assembles and validates the accepted proposal with trusted metadata before saving.
- `PlaylistValidator` in `generation/validation.py` binds a validation context to the private `_validate_playlist()` checks. Inject it into `PlaylistAgent` so tool feedback and final validation share the same evidence. `assemble_daily_record()` retains its mandatory host-side check.
- Tests inject a fake Anthropic client into `ClaudeService(...)` or an `agent_client_factory` into `PlaylistAgent(...)`; respx can't intercept the Anthropic SDK's HTTP layer.

## Spotify requests

- Generation automatically publishes after shared validation, outside agent tools. Create publicly in every storage environment, checkpoint identity before insertion, and verify the name, public visibility, and all ten ordered tracks. Test playlists are deleted manually; published records are visible on the website. New daily and Spotify titles use `YYYY-MM-DD — <2–4 word generated title>`; historical titles stay unchanged.
- Publication failures preserve previous daily bytes and retain created playlists. `publication.json` and diagnostics retain progress and known Spotify identity. Do not retry ambiguous mutations, delete, resume, or roll back automatically. Artifact recovery uploads only and never calls Spotify or republishes daily data. S3 website refresh occurs 60 seconds after each attempt.
- Every Spotify Web API call lives in `src/the_bot_radio/services/spotify_service.py` (`SpotifyService`, a thin httpx client; no SDK). It uses the Feb 2026 Dev Mode endpoints: `POST /me/playlists`, `/playlists/{id}/items` (`items.items[].item`), search `limit` ≤ 10.
- Credentials: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN` (from `scripts/spotify_auth.py`). Quirks are in `docs/spotify.md`.
- Tests mock HTTP with respx; fixtures in `tests/services/fixtures/spotify/` are trimmed real responses.

## Input services (`src/the_bot_radio/services/inputs/`)

- One module per input, a class with `key`/`label` ClassVars and `async fetch(ctx) -> Reading`, registered in `SERVICES` (display order) in `__init__.py`.
- On failure, raise — `runner.read_all` turns any exception or timeout into `status="skipped"` so one bad source never blocks the run. Return `status="stale"` yourself when data is present but outdated.
- Use `ctx.http` (shared client; NWS requires its User-Agent) and build results with `ctx.reading(self, value, data=..., raw=...)`. `value` is a short chip-sized display string; `data` holds normalized fields; `raw` is a trimmed source payload.
- Services return no effect/weight — those are added later by the generation step.
- Test fixtures in `tests/services/fixtures/<key>/` are trimmed real API responses, not invented payloads. Each input's API quirks, chosen station/IDs and status rules are documented in `docs/inputs/<key>.md`; update it when behavior changes.
