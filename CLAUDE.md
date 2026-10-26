## Commands

- Lint/format: `uv run ruff check . && uv run ruff format .` (line length 100; `docs/` is excluded)
- Live API tests are skipped by default. Run them with `RUN_LIVE=1 uv run pytest -m live -s`; `tests/services/test_runner.py::test_live_read_all` prints every input's reading for today.

## Conventions

- All dates/times are America/New_York (`web/config.py` `TZ`). Pass timezone-aware datetimes; in input services use `ctx.run_date` / `ctx.now`, never `date.today()` / `datetime.now()`.
- Async tests use anyio (not pytest-asyncio) and mock HTTP with respx. Each test module must set `pytestmark = pytest.mark.anyio` itself — a `pytestmark` in conftest.py does not apply.
- The site and the (future) generation pipeline share one contract: one JSON file per day in `data/days/YYYY-MM-DD.json`, schema in `src/the_bot_radio/web/models.py`. The newest file is the Today page; the store reloads on file changes and skips invalid files.
- Weekday/date labels are always derived from dates (`web/view_helpers.py`), never hard-coded in templates or data.
- Page CSS lives in the single `static/css/site.css` (tokens on `:root`, dark only). Templates use Jinja macros from `templates/partials/`; derived values come from `view_helpers` (exposed to templates as `h`).

## LLM requests

- Every call to Claude lives in `src/the_bot_radio/services/claude_service.py` (`ClaudeService`, one method per request type). Prompts are module constants there. Credentials come from `ANTHROPIC_API_KEY` (or an `ant auth login` profile).
- Tests inject a fake client into `ClaudeService(...)`; respx can't intercept the Anthropic SDK's HTTP layer.

## Spotify requests

- Every Spotify Web API call lives in `src/the_bot_radio/services/spotify_service.py` (`SpotifyService`, a thin httpx client; no SDK). It uses the Feb 2026 Dev Mode endpoints: `POST /me/playlists`, `/playlists/{id}/items` (`items.items[].item`), search `limit` ≤ 10.
- Credentials: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN` (from `scripts/spotify_auth.py`). Quirks are in `docs/spotify.md`.
- Tests mock HTTP with respx; fixtures in `tests/services/fixtures/spotify/` are trimmed real responses.

## Input services (`src/the_bot_radio/services/inputs/`)

- One module per input, a class with `key`/`label` ClassVars and `async fetch(ctx) -> Reading`, registered in `SERVICES` (display order) in `__init__.py`.
- On failure, raise — `runner.read_all` turns any exception or timeout into `status="skipped"` so one bad source never blocks the run. Return `status="stale"` yourself when data is present but outdated.
- Use `ctx.http` (shared client; NWS requires its User-Agent) and build results with `ctx.reading(self, value, data=..., raw=...)`. `value` is a short chip-sized display string; `data` holds normalized fields; `raw` is a trimmed source payload.
- Services return no effect/weight — those are added later by the generation step.
- Test fixtures in `tests/services/fixtures/<key>/` are trimmed real API responses, not invented payloads. Each input's API quirks, chosen station/IDs and status rules are documented in `docs/inputs/<key>.md`; update it when behavior changes.
