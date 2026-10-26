# The Bot Radio

thebotradio.com: an AI-run homage to Brooklyn's independent online radio. Every morning at 06:00 ET
one bot DJ reads Greenpoint (weather, the G train, the tide, Citi Bikes, yesterday's 311 calls…)
and drops one Spotify playlist, with liner notes explaining every pick.

This repo currently contains the website: a server-rendered FastAPI + Jinja2 app that renders
whatever daily records are in `data/days/`. The generation pipeline is not built yet; the site
runs on fixture data.

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

## Test and lint

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Data

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
