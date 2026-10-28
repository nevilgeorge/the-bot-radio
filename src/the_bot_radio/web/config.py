import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parents[2]

# Local secrets (see .env.example). Variables already set in the shell win.
load_dotenv(REPO_ROOT / ".env")

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

# About page "Data sources" table: (source, used for).
DATA_SOURCES = (
    ("National Weather Service", "Weather over Greenpoint"),
    ("MTA real-time feeds", "G train delays and service changes"),
    ("NOAA Tides & Currents", "High and low tide"),
    ("Citi Bike public feed", "Bikes docked at Nassau Av"),
    ("NYC Open Data", "Yesterday's 311 requests in 11222"),
    ("Calculated locally", "Sunset, day of week, the DJ roll"),
    ("Spotify", "Playlists and playback"),
)
