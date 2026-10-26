import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parents[2]

DATA_DIR = Path(os.environ.get("BOT_RADIO_DATA_DIR", REPO_ROOT / "data"))
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

TZ = ZoneInfo("America/New_York")
GENERATION_TIME = time(6, 0)
GENERATION_TIME_LABEL = "06:00 ET"

SITE_NAME = "The Bot Radio"
SITE_URL = os.environ.get("BOT_RADIO_SITE_URL", "https://thebotradio.com")
SITE_DESCRIPTION = (
    "An AI-run radio homage. Every morning at 06:00 ET a bot DJ reads Greenpoint "
    "and drops one new Spotify playlist, with liner notes explaining every pick."
)

INSTAGRAM_URL = os.environ.get("BOT_RADIO_INSTAGRAM_URL", "#")
SPOTIFY_PROFILE_URL = os.environ.get("BOT_RADIO_SPOTIFY_PROFILE_URL", "#")
