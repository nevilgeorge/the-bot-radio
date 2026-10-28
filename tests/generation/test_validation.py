"""Rule enforcement and trusted daily-record assembly."""

from dataclasses import replace
from datetime import date, datetime
from typing import Any

import pytest

from the_bot_radio.catalog import CatalogTrack
from the_bot_radio.generation.models import PlaylistMood, PlaylistProposal, ValidationContext
from the_bot_radio.generation.validation import (
    PlaylistValidator,
    assemble_daily_record,
)
from the_bot_radio.services.inputs.base import TZ, Reading
from the_bot_radio.services.spotify_service import SpotifyTrack

NOW = datetime(2026, 10, 27, 6, tzinfo=TZ)


def make_inputs() -> tuple[dict[str, Any], ValidationContext]:
    """Build ten distinct canonical recordings and trusted neighborhood evidence."""
    tracks = [
        {
            "position": position,
            "spotify_id": str(position).zfill(22),
            "spotify_url": f"https://open.spotify.com/track/{str(position).zfill(22)}",
            "title": f"Song {position}",
            "artists": [f"Artist {position}", "Shared featured artist"],
            "duration_ms": 200000 + position,
            "reason": "Fits today's atmosphere.",
            "wildcard": position > 5,
        }
        for position in range(1, 11)
    ]
    catalog = {
        track["spotify_id"]: CatalogTrack(
            **{key: track[key] for key in ("spotify_id", "title", "artists", "duration_ms")},
            spotify_url=track["spotify_url"],
            album="An album",
        )
        for track in tracks[:5]
    }
    search_results = {
        track["spotify_id"]: SpotifyTrack(
            id=track["spotify_id"],
            uri=f"spotify:track:{track['spotify_id']}",
            name=track["title"],
            artists=track["artists"],
            album="An album",
            duration_ms=track["duration_ms"],
            explicit=False,
            external_url=None,
        )
        for track in tracks[5:]
    }
    readings = (
        Reading(
            key="dj_roll", label="DJ", value="DJ One", data={"persona_id": "dj-1"}, fetched_at=NOW
        ),
        Reading(key="weather", label="Weather", value="Rain", data={"temp_f": 52}, fetched_at=NOW),
        Reading(
            key="tide", label="Tide", value="", status="skipped", error="Timeout", fetched_at=NOW
        ),
    )
    proposal = {
        "title": "Morning rain",
        "description": "Ten tracks for Greenpoint.",
        "liner_notes": ["Slow down with the neighborhood."],
        "tracks": tracks,
        "effects": [
            {"key": "dj_roll", "effect": "A warm selection", "weight": None},
            {"key": "weather", "effect": "Softer textures", "weight": 0.5},
            {"key": "tide", "effect": "", "weight": None},
        ],
    }
    context = ValidationContext(
        run_date=date(2026, 10, 27),
        dj_id="dj-1",
        catalog=catalog,
        recent_track_ids=frozenset(),
        readings=readings,
        mood=PlaylistMood(tempo_bpm=85, energy=3, brightness=4, obscurity=7),
        search_results=search_results,
    )
    return proposal, context


def violation_codes(proposal: dict[str, Any], context: ValidationContext) -> set[str]:
    """Return failure codes while asserting a proposal is rejected."""
    result = PlaylistValidator(context).validate_playlist(proposal)
    assert not result.valid
    return {violation.code for violation in result.violations}


def test_valid_proposal_allows_shared_featured_artists_and_missing_search_urls() -> None:
    proposal, context = make_inputs()
    assert PlaylistValidator(context).validate_playlist(proposal).valid
    assert (
        PlaylistValidator(context)
        .validate_playlist(PlaylistProposal.model_validate(proposal))
        .valid
    )


@pytest.mark.parametrize("title", ["Morning", "One two three four five", "   "])
def test_title_requires_two_to_four_whitespace_words(title: str) -> None:
    proposal, context = make_inputs()
    proposal["title"] = title
    assert "invalid_title_length" in violation_codes(proposal, context)


@pytest.mark.parametrize("title", ["Morning\train", "A slow morning", "A very slow morning"])
def test_title_accepts_two_to_four_whitespace_words(title: str) -> None:
    proposal, context = make_inputs()
    proposal["title"] = title
    assert PlaylistValidator(context).validate_playlist(proposal).valid


@pytest.mark.parametrize("title", ["Song 1", "  sOnG\t 1  ", "SONG 10"])
def test_title_cannot_copy_selected_track_after_normalization(title: str) -> None:
    proposal, context = make_inputs()
    proposal["title"] = title
    assert "title_matches_track" in violation_codes(proposal, context)


def test_validator_observes_search_evidence_added_after_construction() -> None:
    """Keep tool search evidence live in the validator's wrapped run context."""
    proposal, context = make_inputs()
    search_results = context.search_results.copy()
    context.search_results.clear()
    validator = PlaylistValidator(context)

    initial_result = validator.validate_playlist(proposal)
    assert not initial_result.valid
    assert {violation.code for violation in initial_result.violations} == {"unverified_wildcard"}
    assert validator.context is context

    context.search_results.update(search_results)
    assert validator.validate_playlist(proposal).valid
    assert validator.validate_playlist(PlaylistProposal.model_validate(proposal)).valid


@pytest.mark.parametrize("track_count", [0, 9, 11])
def test_wrong_track_count_returns_structured_errors(track_count: int) -> None:
    proposal, context = make_inputs()
    proposal["tracks"] = (proposal["tracks"] + [proposal["tracks"][0]])[:track_count]
    assert "invalid_structure" in violation_codes(proposal, context)


def test_missing_field_has_position_and_field() -> None:
    proposal, context = make_inputs()
    del proposal["tracks"][2]["reason"]
    result = PlaylistValidator(context).validate_playlist(proposal)
    assert not result.valid
    assert result.violations[0].position == 3
    assert result.violations[0].field == "tracks.2.reason"


def test_mutated_typed_proposal_is_reparsed_before_checks() -> None:
    proposal, context = make_inputs()
    parsed = PlaylistProposal.model_validate(proposal)
    parsed.tracks[0].artists.clear()
    result = PlaylistValidator(context).validate_playlist(parsed)
    assert not result.valid
    assert result.violations[0].code == "invalid_structure"


def test_positions_split_duplicates_and_recent_history() -> None:
    proposal, context = make_inputs()
    proposal["tracks"][1] = {**proposal["tracks"][0], "position": 1}
    proposal["tracks"][5]["wildcard"] = False
    context = replace(context, recent_track_ids=frozenset({proposal["tracks"][0]["spotify_id"]}))
    assert {
        "invalid_positions",
        "invalid_split",
        "duplicate_track",
        "duplicate_artist",
        "recently_played",
    } <= violation_codes(proposal, context)


def test_primary_artist_comparison_normalizes_case_and_whitespace() -> None:
    proposal, context = make_inputs()
    proposal["tracks"][1]["artists"][0] = "  ARTIST   1  "
    assert "duplicate_artist" in violation_codes(proposal, context)


def test_wildcards_must_be_outside_catalog_and_verified() -> None:
    proposal, context = make_inputs()
    proposal["tracks"][0]["wildcard"] = True
    del context.search_results[proposal["tracks"][5]["spotify_id"]]
    assert {"wildcard_in_catalog", "unverified_wildcard"} <= violation_codes(proposal, context)


def test_catalog_tracks_must_exist() -> None:
    proposal, context = make_inputs()
    del context.catalog[proposal["tracks"][0]["spotify_id"]]
    assert "unknown_catalog_track" in violation_codes(proposal, context)


@pytest.mark.parametrize("position", [0, 5])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("title", "Wrong recording"),
        ("artists", ["Wrong artist"]),
        ("duration_ms", 300000),
        ("spotify_url", "https://example.com/track"),
    ],
)
def test_canonical_metadata_cannot_be_invented(position: int, field: str, replacement: Any) -> None:
    proposal, context = make_inputs()
    proposal["tracks"][position][field] = replacement
    assert "metadata_mismatch" in violation_codes(proposal, context)


def test_effects_must_cover_exactly_the_readings() -> None:
    proposal, context = make_inputs()
    proposal["effects"] = [
        proposal["effects"][0],
        proposal["effects"][0],
        {"key": "invented", "effect": "Bright", "weight": 0.3},
    ]
    assert {"duplicate_effect", "unknown_effect", "missing_effect"} <= violation_codes(
        proposal, context
    )


def test_skipped_effect_and_dj_weight_are_rejected() -> None:
    proposal, context = make_inputs()
    proposal["effects"][0]["weight"] = 0.2
    proposal["effects"][2]["effect"] = "Tides made us dance"
    assert {"dj_roll_weight", "unavailable_input_effect"} <= violation_codes(proposal, context)


def test_stale_311_metadata_is_not_usable_evidence() -> None:
    proposal, context = make_inputs()
    reading = Reading(
        key="nyc_311_yesterday",
        label="311",
        value="No data yet",
        status="stale",
        data={"date": None, "requested_date": "2026-10-26"},
        fetched_at=NOW,
    )
    context = replace(context, readings=(*context.readings, reading))
    proposal["effects"].append({"key": reading.key, "effect": "Noise raised energy", "weight": 0.3})
    assert "unavailable_input_effect" in violation_codes(proposal, context)
    proposal["effects"][-1].update(effect="", weight=0)
    assert PlaylistValidator(context).validate_playlist(proposal).valid


def test_stale_reading_with_data_can_influence_selection() -> None:
    proposal, context = make_inputs()
    readings = list(context.readings)
    readings[1] = readings[1].model_copy(update={"status": "stale"})
    assert (
        PlaylistValidator(replace(context, readings=tuple(readings)))
        .validate_playlist(proposal)
        .valid
    )


def test_assembly_uses_trusted_evidence_and_pending_publication() -> None:
    proposal, context = make_inputs()
    context.readings[1].raw["source"] = "NWS"
    record = assemble_daily_record(
        PlaylistProposal.model_validate(proposal), context, "Prompt", "Model", NOW
    )
    assert record.date == context.run_date
    assert record.title == "2026-10-27 — Morning rain"
    assert record.dj == context.dj_id
    assert record.mood == context.mood
    assert record.publication_status == "pending"
    assert record.spotify_playlist_id is None
    assert record.tracks[0].artist == "Artist 1, Shared featured artist"
    assert record.inputs[1].raw == {"source": "NWS"}
    assert record.inputs[2].error == "Timeout"
    assert record.inputs[2].effect == ""
    assert record.generated_at == NOW


def test_assembly_rejects_output_changed_after_a_successful_tool_call() -> None:
    proposal, context = make_inputs()
    parsed = PlaylistProposal.model_validate(proposal)
    assert PlaylistValidator(context).validate_playlist(parsed).valid
    parsed.tracks[0].duration_ms = 1
    with pytest.raises(ValueError, match="canonical duration_ms"):
        assemble_daily_record(parsed, context, "Prompt", "Model", NOW)
