# The Bot Radio

thebotradio.com: an AI-run spoof of Brooklyn's independent online radio. Every morning at 06:00 ET
one bot DJ reads Greenpoint (weather, the G train, the tide, Citi Bikes, yesterday's 311 calls…)
and drops one Spotify playlist, with liner notes explaining every pick.

This repo contains a FastAPI + Jinja2 website and a Claude Agent SDK harness that generates
validated daily playlist JSON. Spotify playlist publishing is a separate step. The website
serves published records and legacy fixtures from `data/days/`; pending generations stay hidden.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.13 (uv installs it automatically from `.python-version`)

## Run

```bash
uv sync                 # install dependencies
uv run the-bot-radio    # serve on http://127.0.0.1:8000 with auto-reload
```

Or run uvicorn directly (e.g. to change host/port):

```bash
uv run uvicorn the_bot_radio.web.main:app --reload --port 8000
```

Pages: `/` (Today), `/index` (archive, `?dj=resident-02`, `?month=2026-10`), `/index/2026-10-26`
(daily playlist), `/inputs`, `/bots`, `/about`. Health check: `/healthz`.


## Generate a daily playlist

Populate `data/shared_catalog.jsonl` with the offline collector and configure
`ANTHROPIC_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and
`SPOTIFY_REFRESH_TOKEN` in `.env` (see [Spotify setup](docs/spotify.md)). Then run:

```bash
uv run the-bot-radio-generate
uv run the-bot-radio-generate --date 2026-10-27 --data-dir /path/to/data
```

The command selects one resident, reads the neighborhood, scores mood, and uses a read-only
agent with Spotify search and playlist validation. It writes exactly ten ordered tracks:
five from the catalog and five verified discoveries outside it. Primary artists are unique,
and track IDs from explicitly published playlists in the preceding 30 days are excluded.
Featured artists may recur. Legacy fixtures and pending generations do not count as history.

The output is `data/days/YYYY-MM-DD.json` with `publication_status: "pending"` and a null
Spotify playlist ID. **This command does not create a Spotify playlist.** Rerunning an accepted
day is a no-op; an invalid existing file is preserved and reported as an error. Same-day runs
are locked against concurrent generation.

To watch agent messages, tool activity, and run events as JSON lines in your terminal:

```bash
uv run the-bot-radio-generate --verbose
```

Events stream to stderr with the same credential redaction as `transcript.jsonl`;
stdout still prints the output path.

Date precedence is `--date`, then `RUN_DATE`, then today in New York. Historical dates affect
calendar and prediction inputs; weather, train, and bike feeds remain live observations.
Missing history is recorded explicitly, so an initial run can proceed without prior playlists.

Agent defaults are 40 turns, a $5 estimated budget, and a 600-second session deadline
for one generation task. The agent can validate and revise through tools during that task;
invalid final output fails without a host repair prompt. Override with `--max-turns`, `--budget-usd`,
`--deadline-seconds`, and `--model`. Budget estimates can overshoot; mood scoring is separate
and uses the existing service model. The deadline covers the agent session, not input setup.

Each run lives under `data/runs/YYYY-MM-DD/<run-id>/`, outside the website and ignored
by Git. `proposal.json` stores a schema-valid final response from a successful SDK result,
including proposals rejected by playlist rules; `transcript.jsonl`
records the conversation, and `search_results.json` preserves Spotify verification evidence.
`diagnostics.json` combines run status, raw input readings, mood usage, and failure details.
The agent can read only `workspace/`, containing `context.json`, `catalog.jsonl`, `history/`,
and `schemas/`. The selected persona is supplied in the prompt; `data/personas.json` stays
at the source. The full layout and file lifecycle are documented in the module docstring of
`src/the_bot_radio/generation/daily.py`. Earlier run layouts remain untouched.
The schemas in `schemas/` are generated from the Pydantic contracts; only the proposal schema
is supplied in each agent workspace. `generation/agent.py` owns `PlaylistAgent` and the playlist system
prompt; `ClaudeService` handles standalone requests such as mood scoring. A `PlaylistValidator`
binds the run context to the shared validation checks and is injected into the agent for tool
feedback and final proposal validation. The agent returns a proposal and archived prompt. The daily job adds trusted metadata once, validates the
assembled record, and atomically saves it.

Run opt-in live qualification in temporary data without publishing to Spotify:

```bash
RUN_LIVE=1 uv run pytest -s tests/generation/test_daily.py::test_live_generation_writes_only_a_pending_temporary_day
```

## Test and lint

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Data

To import shared catalog playlists and collect audio features, see
[song feature collection](docs/song_features.md).

The site reads JSON from `data/` (override with `BOT_RADIO_DATA_DIR`):

- `data/days/YYYY-MM-DD.json`: one record per day, written by the pipeline. The newest file is
  the Today page. Files are picked up without a restart; invalid files are logged and skipped.
- `data/personas.json`: the bot DJs.
- `data/inputs.json`: descriptions of each input for the Inputs page.

The schema is in `src/the_bot_radio/web/models.py`.

## Configuration

Optional environment variables (see `src/the_bot_radio/web/config.py`):

| Variable | Default |
|---|---|
| `BOT_RADIO_DATA_DIR` | `./data` |
| `BOT_RADIO_SITE_URL` | `https://thebotradio.com` |
| `BOT_RADIO_INSTAGRAM_URL` | `#` |
| `BOT_RADIO_SPOTIFY_PROFILE_URL` | `#` |

## Layout

```
src/the_bot_radio/web/   FastAPI app: routes, store, view helpers, templates, static CSS/JS
data/                    personas, input descriptions, daily records
tests/                   pytest suite
docs/                    project docs
```
