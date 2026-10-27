"""Strict generation contracts, schema exports, and trusted validation state."""

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from the_bot_radio.catalog import CatalogTrack
from the_bot_radio.services.inputs.base import Reading
from the_bot_radio.services.spotify_service import SpotifyTrack

NonemptyString = Annotated[str, Field(min_length=1)]
SpotifyId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{22}$")]


class GenerationModel(BaseModel):
    """Reject unexpected fields and nonfinite scores at generation boundaries."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlaylistMood(GenerationModel):
    """The fixed, separately computed mood used by the selection agent."""

    tempo_bpm: int = Field(ge=60, le=160)
    energy: int = Field(ge=0, le=10)
    brightness: int = Field(ge=0, le=10)
    obscurity: int = Field(ge=0, le=10)


class PlaylistTrack(GenerationModel):
    """A canonical recording plus the agent's sequencing and explanation."""

    position: int = Field(ge=1, le=10)
    spotify_id: SpotifyId
    spotify_url: NonemptyString
    title: NonemptyString
    artists: list[NonemptyString] = Field(min_length=1)
    duration_ms: int = Field(gt=0)
    reason: NonemptyString
    wildcard: bool


class InputEffect(GenerationModel):
    """An agent-authored interpretation referencing a trusted reading key."""

    key: NonemptyString
    effect: str
    weight: float | None = Field(ge=0, le=1)


class PlaylistProposal(GenerationModel):
    """Only agent-authored fields; runtime and publication metadata are host-owned."""

    title: NonemptyString
    description: str = Field(max_length=300)
    liner_notes: list[NonemptyString] = Field(min_length=1)
    tracks: list[PlaylistTrack] = Field(min_length=10, max_length=10)
    effects: list[InputEffect]


class DailyTrack(PlaylistTrack):
    """A persisted recording with the backward-compatible display credit."""

    artist: NonemptyString


class DailyInput(GenerationModel):
    """Trusted evidence with its agent-authored effect and weight."""

    key: str
    label: str
    value: str
    status: Literal["ok", "stale", "skipped"]
    data: dict[str, Any]
    raw: dict[str, Any]
    error: str | None
    fetched_at: AwareDatetime
    effect: str
    weight: float | None = Field(ge=0, le=1)


class DailyRecord(GenerationModel):
    """The accepted daily file, before or after deterministic publication."""

    date: date
    dj: NonemptyString
    title: NonemptyString
    description: str = Field(max_length=300)
    liner_notes: list[NonemptyString] = Field(min_length=1)
    tracks: list[DailyTrack] = Field(min_length=10, max_length=10)
    inputs: list[DailyInput]
    mood: PlaylistMood
    publication_status: Literal["pending", "published"]
    spotify_playlist_id: SpotifyId | None
    prompt: NonemptyString
    model: NonemptyString
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def check_record_consistency(self) -> "DailyRecord":
        """Reject invalid publication state and internally inconsistent track metadata."""
        if (self.publication_status == "published") != (self.spotify_playlist_id is not None):
            raise ValueError(
                "published records require a playlist ID; pending records require null"
            )
        if [track.position for track in self.tracks] != list(range(1, 11)):
            raise ValueError("track positions must be 1 through 10 in playback order")
        if sum(track.wildcard for track in self.tracks) != 5:
            raise ValueError("exactly five wildcards are required")
        if len({track.spotify_id for track in self.tracks}) != 10:
            raise ValueError("duplicate track IDs")
        primary_artists = [normalize_artist(track.artists[0]) for track in self.tracks]
        if len(set(primary_artists)) != 10:
            raise ValueError("duplicate primary artists")
        for track in self.tracks:
            if track.artist != ", ".join(track.artists):
                raise ValueError("display artist must match canonical credits")
            if track.spotify_url != f"https://open.spotify.com/track/{track.spotify_id}":
                raise ValueError("Spotify URL must match track ID")
        return self


class Violation(GenerationModel):
    """One actionable structural or musical-rule failure."""

    code: str
    message: str
    position: int | None = None
    field: str | None = None


class ValidationResult(GenerationModel):
    """Tool feedback and the mandatory acceptance decision."""

    valid: bool
    violations: list[Violation] = Field(default_factory=list)


@dataclass(frozen=True)
class ValidationContext:
    """Trusted references supplied by setup, never by agent tool arguments."""

    run_date: date
    dj_id: str
    catalog: dict[str, CatalogTrack]
    recent_track_ids: frozenset[str]
    readings: tuple[Reading, ...]
    mood: PlaylistMood
    search_results: dict[str, SpotifyTrack] = field(default_factory=dict)


def normalize_artist(name: str) -> str:
    """Compare primary credits without case or whitespace differences."""
    return " ".join(name.split()).casefold()


def write_schemas(directory: Path) -> None:
    """Export the same contracts used by SDK output parsing and daily persistence."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, model in (
        ("playlist_proposal.schema.json", PlaylistProposal),
        ("daily_playlist.schema.json", DailyRecord),
    ):
        (directory / filename).write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )
