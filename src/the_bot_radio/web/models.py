from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class Persona(BaseModel):
    id: str  # "resident-01"
    name: str  # display name
    bio: str = ""  # persona description shown on /bots
    agent_instructions: str = ""  # playlist selection and sequencing guidance
    speaking_voice: str = ""  # voice for titles, liner notes, and track explanations


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
    spotify_url: str | None = None
    artists: list[str] = Field(default_factory=list)
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
    description: str = ""
    publication_status: Literal["pending", "published"] | None = None
    spotify_playlist_id: str | None
    spotify_playlist_url: str | None = None
    spotify_snapshot_id: str | None = None
    spotify_playlist_public: bool | None = None
    published_at: AwareDatetime | None = None
    prompt: str  # exact prompt sent to the model (shown on the day page)
    model: str
    generated_at: datetime  # timezone-aware


class InputDescription(BaseModel):  # data/inputs.json, for the Inputs page
    key: str
    name: str  # "The G train"
    source: str  # "MTA real-time feeds"
    reads: str  # what it reads, one sentence
    effect: str  # what it does to the music
