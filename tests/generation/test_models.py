"""Strict output contracts and the generated schemas exposed to the agent."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from the_bot_radio.generation.models import (
    DailyRecord,
    PlaylistMood,
    PlaylistProposal,
    PlaylistTrack,
    write_schemas,
)
from the_bot_radio.web.models import Day

SCHEMA_DIRECTORY = Path(__file__).resolve().parents[2] / "schemas"
REPO_DATA = Path(__file__).resolve().parents[2] / "data"


def daily_record_payload() -> dict[str, Any]:
    """Return a complete pending record without external service dependencies."""
    return {
        "date": "2026-10-27",
        "dj": "resident-01",
        "title": "Morning light",
        "description": "Ten tracks for the neighborhood.",
        "liner_notes": ["A gentle opening."],
        "tracks": [
            {
                "position": position,
                "spotify_id": str(position).zfill(22),
                "spotify_url": f"https://open.spotify.com/track/{str(position).zfill(22)}",
                "title": f"Track {position}",
                "artists": [f"Artist {position}"],
                "artist": f"Artist {position}",
                "duration_ms": 200000,
                "reason": "Matches the mood.",
                "wildcard": position > 5,
            }
            for position in range(1, 11)
        ],
        "inputs": [],
        "mood": {"tempo_bpm": 85, "energy": 4, "brightness": 3, "obscurity": 7},
        "publication_status": "pending",
        "spotify_playlist_id": None,
        "prompt": "Select today's ten tracks.",
        "model": "test-model",
        "generated_at": "2026-10-27T06:00:00-04:00",
    }


def test_daily_record_json_round_trip_and_website_compatibility() -> None:
    record = DailyRecord.model_validate(daily_record_payload())
    assert DailyRecord.model_validate_json(record.model_dump_json()) == record
    website_day = Day.model_validate_json(record.model_dump_json())
    assert website_day.publication_status == "pending"
    assert website_day.spotify_playlist_id is None
    assert website_day.tracks[0].spotify_url == record.tracks[0].spotify_url
    assert website_day.tracks[0].artists == record.tracks[0].artists


def test_published_metadata_round_trip_and_private_website_compatibility() -> None:
    payload = daily_record_payload()
    payload.update(
        publication_status="published",
        spotify_playlist_id="1" * 22,
        spotify_playlist_url=f"https://open.spotify.com/playlist/{'1' * 22}",
        spotify_snapshot_id="verified-snapshot",
        spotify_playlist_public=False,
        published_at="2026-10-27T06:01:00-04:00",
    )
    record = DailyRecord.model_validate(payload)
    assert DailyRecord.model_validate_json(record.model_dump_json()) == record
    website_day = Day.model_validate_json(record.model_dump_json())
    assert website_day.spotify_playlist_url == record.spotify_playlist_url
    assert website_day.spotify_snapshot_id == "verified-snapshot"
    assert website_day.spotify_playlist_public is False
    assert website_day.published_at == record.published_at


@pytest.mark.parametrize("publication_status", ["pending", "published"])
def test_historical_records_do_not_require_new_publication_metadata(
    publication_status: str,
) -> None:
    payload = daily_record_payload()
    payload.update(
        publication_status=publication_status,
        spotify_playlist_id="1" * 22 if publication_status == "published" else None,
    )
    record = DailyRecord.model_validate(payload)
    assert record.title == "Morning light"
    assert record.spotify_playlist_url is None
    assert record.spotify_snapshot_id is None
    assert record.spotify_playlist_public is None
    assert record.published_at is None


@pytest.mark.parametrize("record_model", [DailyRecord, Day])
def test_publication_timestamp_must_be_timezone_aware(
    record_model: type[DailyRecord | Day],
) -> None:
    payload = daily_record_payload()
    payload["published_at"] = "2026-10-27T06:01:00"
    with pytest.raises(ValidationError):
        record_model.model_validate(payload)


def test_generated_schema_files_match_canonical_models(tmp_path: Path) -> None:
    write_schemas(tmp_path)
    for filename, model in (
        ("playlist_proposal.schema.json", PlaylistProposal),
        ("daily_playlist.schema.json", DailyRecord),
    ):
        expected_schema = model.model_json_schema()
        assert json.loads((tmp_path / filename).read_text()) == expected_schema
        assert json.loads((SCHEMA_DIRECTORY / filename).read_text()) == expected_schema


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tempo_bpm", 59),
        ("tempo_bpm", 161),
        ("energy", -1),
        ("energy", 11),
        ("brightness", -1),
        ("brightness", 11),
        ("obscurity", -1),
        ("obscurity", 11),
    ],
)
def test_mood_rejects_out_of_range_values(field: str, value: int) -> None:
    mood = daily_record_payload()["mood"]
    mood[field] = value
    with pytest.raises(ValidationError):
        PlaylistMood.model_validate(mood)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spotify_id", "PLACEHOLDER"),
        ("title", ""),
        ("artists", []),
        ("duration_ms", 0),
        ("reason", ""),
        ("position", 0),
        ("position", 11),
    ],
)
def test_track_requires_real_metadata(field: str, value: Any) -> None:
    track = daily_record_payload()["tracks"][0]
    del track["artist"]
    track[field] = value
    with pytest.raises(ValidationError):
        PlaylistTrack.model_validate(track)


def test_proposal_excludes_host_owned_metadata() -> None:
    payload = daily_record_payload()
    proposal = {key: payload[key] for key in ("title", "description", "liner_notes")}
    proposal["tracks"] = [
        {key: value for key, value in track.items() if key != "artist"}
        for track in payload["tracks"]
    ]
    proposal["effects"] = []
    assert PlaylistProposal.model_validate(proposal)
    proposal["spotify_playlist_id"] = "1" * 22
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlaylistProposal.model_validate(proposal)


@pytest.mark.parametrize("track_count", [9, 11])
def test_record_requires_ten_tracks(track_count: int) -> None:
    payload = daily_record_payload()
    payload["tracks"] = (payload["tracks"] + [payload["tracks"][0]])[:track_count]
    with pytest.raises(ValidationError):
        DailyRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("status", "playlist_id", "valid"),
    [
        ("pending", None, True),
        ("published", "1" * 22, True),
        ("pending", "1" * 22, False),
        ("published", None, False),
    ],
)
def test_publication_state_requires_consistent_playlist_id(
    status: str, playlist_id: str | None, valid: bool
) -> None:
    payload = daily_record_payload()
    payload.update(publication_status=status, spotify_playlist_id=playlist_id)
    if valid:
        assert DailyRecord.model_validate(payload)
    else:
        with pytest.raises(ValidationError, match="published records require"):
            DailyRecord.model_validate(payload)


def test_record_requires_timezone_aware_timestamp() -> None:
    payload = daily_record_payload()
    payload["generated_at"] = "2026-10-27T06:00:00"
    with pytest.raises(ValidationError):
        DailyRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("position", 3, "positions"),
        ("wildcard", True, "five wildcards"),
        ("artist", "Untrusted credit", "display artist"),
        ("spotify_url", "https://example.com/song", "Spotify URL"),
    ],
)
def test_record_rejects_internal_inconsistency(field: str, replacement: Any, message: str) -> None:
    payload = daily_record_payload()
    payload["tracks"][0][field] = replacement
    with pytest.raises(ValidationError, match=message):
        DailyRecord.model_validate(payload)


def test_record_rejects_duplicate_recordings() -> None:
    payload = daily_record_payload()
    payload["tracks"][1]["spotify_id"] = payload["tracks"][0]["spotify_id"]
    with pytest.raises(ValidationError, match="duplicate track IDs"):
        DailyRecord.model_validate(payload)


def test_record_rejects_duplicate_primary_artists() -> None:
    payload = daily_record_payload()
    payload["tracks"][1]["artists"] = ["  ARTIST  1 "]
    with pytest.raises(ValidationError, match="duplicate primary artists"):
        DailyRecord.model_validate(payload)


def test_legacy_website_fixtures_remain_display_compatible() -> None:
    # Only the checked-in legacy fixtures belong to this assertion; local generation
    # also writes modern daily records into this gitignored directory.
    paths = [REPO_DATA / "days" / f"2026-10-{day:02d}.json" for day in range(15, 27)]
    for path in paths:
        day = Day.model_validate_json(path.read_text())
        assert day.publication_status is None
        assert day.spotify_playlist_id == "PLACEHOLDER"
        assert len(day.tracks) == len(json.loads(path.read_text())["tracks"])
        assert len(day.tracks) > 10
