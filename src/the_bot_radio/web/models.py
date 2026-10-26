from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Persona(BaseModel):
    id: str  # "resident-01"
    name: str  # display name, e.g. "[Resident 01]" until personas are defined
    bio: str = ""  # persona description shown on /bots
    crate_size: int | None = None


class InputReading(BaseModel):
    # "weather" | "g_train" | "sunset" | "tide" | "citibike" | "nyc_311_yesterday"
    # | "day_of_week" | "dj_roll"
    key: str
    label: str  # "Weather · Greenpoint"
    value: str  # human display string: "52°F, overcast"
    effect: str  # "→ warmer, slower picks"
    weight: float | None = None  # 0–1; None for dj_roll
    status: Literal["ok", "skipped", "stale"] = "ok"
    raw: dict = Field(default_factory=dict)  # untouched source payload, not rendered


class Mood(BaseModel):
    tempo_bpm: int  # average BPM, e.g. 84
    energy: int  # 0–10
    brightness: int  # 0–10
    obscurity: int  # 0–10


class Track(BaseModel):
    position: int
    title: str
    artist: str
    duration_ms: int
    spotify_id: str
    reason: str  # the bot's one-line explanation
    wildcard: bool = False  # picked from the open catalog, not the DJ's crate


class Day(BaseModel):
    date: date
    dj: str  # Persona.id
    title: str  # "Overcast, G running local"
    liner_notes: list[str]  # paragraphs
    inputs: list[InputReading]  # in display order
    mood: Mood
    tracks: list[Track]
    spotify_playlist_id: str
    prompt: str  # exact prompt sent to the model (shown on the day page)
    model: str
    generated_at: datetime  # timezone-aware


class InputDescription(BaseModel):  # data/inputs.json, for the Inputs page
    key: str
    name: str  # "The G train"
    source: str  # "MTA real-time feeds"
    reads: str  # what it reads, one sentence
    effect: str  # what it does to the music
