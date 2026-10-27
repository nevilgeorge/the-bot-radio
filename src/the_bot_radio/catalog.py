"""Shared catalog types and strict JSONL loading for curation and generation."""

from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CatalogModel(BaseModel):
    """Reject unexpected persisted fields instead of silently deleting them on rewrite."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AudioFeatures(CatalogModel):
    """ReccoBeats audio features on their original scales."""

    source: Literal["reccobeats"] = "reccobeats"
    fetched: date
    tempo: float = Field(ge=0)
    energy: float = Field(ge=0, le=1)
    valence: float = Field(ge=0, le=1)
    danceability: float = Field(ge=0, le=1)
    acousticness: float = Field(ge=0, le=1)
    instrumentalness: float = Field(ge=0, le=1)
    liveness: float = Field(ge=0, le=1)
    loudness: float
    speechiness: float = Field(ge=0, le=1)


class Descriptors(CatalogModel):
    """Cached audio descriptors; absence never implies a fabricated musical score."""

    audio: AudioFeatures | None = None


class LookupState(CatalogModel):
    """Track attempted lookups separately from successfully fetched descriptors."""

    status: Literal["ok", "no_data", "error"]
    attempted_at: datetime
    error: str | None = None


class CatalogTrack(CatalogModel):
    """One reusable track record shared by all imported playlists."""

    spotify_id: str = Field(pattern=r"^[A-Za-z0-9]{22}$")
    title: str
    artists: list[str]
    album: str
    duration_ms: int = Field(ge=0)
    spotify_url: str
    source_playlists: list[str] = Field(default_factory=list)
    descriptors: Descriptors = Field(default_factory=Descriptors)
    lookups: dict[Literal["reccobeats"], LookupState] = Field(default_factory=dict)


def load_catalog(path: Path) -> dict[str, CatalogTrack]:
    """Read a strict JSONL snapshot; never silently discard malformed or duplicate records."""
    if not path.exists():
        return {}
    records: dict[str, CatalogTrack] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            track = CatalogTrack.model_validate_json(line)
            if track.spotify_id in records:
                raise ValueError("duplicate Spotify ID")
        except ValueError as error:
            raise ValueError(
                f"invalid catalog at line {line_number}; refusing to overwrite"
            ) from error
        records[track.spotify_id] = track
    return records
