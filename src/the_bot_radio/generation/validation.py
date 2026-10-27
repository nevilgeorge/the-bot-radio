"""Deterministic playlist checks shared by agent feedback and final acceptance."""

from datetime import datetime
from typing import Any

from pydantic import ValidationError

from the_bot_radio.generation.models import (
    DailyInput,
    DailyRecord,
    DailyTrack,
    PlaylistProposal,
    ValidationContext,
    ValidationResult,
    Violation,
    normalize_artist,
)
from the_bot_radio.services.inputs.base import Reading


class PlaylistValidator:
    """Validate proposals against one run's context and retained Spotify evidence."""

    def __init__(self, context: ValidationContext) -> None:
        """Keep the shared context so later search results are available to validation."""
        self.context = context

    def validate_playlist(self, proposal: PlaylistProposal | dict[str, Any]) -> ValidationResult:
        """Return playlist violations using this run's evidence."""
        return _validate_playlist(proposal, self.context)


def _has_usable_data(reading: Reading) -> bool:
    """Distinguish stale evidence from a metadata-only missing-data reading."""
    if not reading.data:
        return False
    if reading.key == "nyc_311_yesterday":
        return reading.data.get("date") is not None
    return True


def _validate_playlist(
    proposal: PlaylistProposal | dict[str, Any], context: ValidationContext
) -> ValidationResult:
    """Return actionable violations without network requests or reference-state mutations."""
    try:
        proposal_data = (
            proposal.model_dump() if isinstance(proposal, PlaylistProposal) else proposal
        )
        parsed_proposal = PlaylistProposal.model_validate(proposal_data)
    except ValidationError as error:
        violations: list[Violation] = []
        for detail in error.errors(include_url=False, include_input=False):
            location = detail["loc"]
            track_index = (
                location[1]
                if len(location) > 1 and location[0] == "tracks" and isinstance(location[1], int)
                else None
            )
            violations.append(
                Violation(
                    code="invalid_structure",
                    message=detail["msg"],
                    position=track_index + 1 if track_index is not None else None,
                    field=".".join(str(part) for part in location),
                )
            )
        return ValidationResult(valid=False, violations=violations)

    violations = []

    def add_violation(
        code: str, message: str, position: int | None = None, field: str | None = None
    ) -> None:
        """Append one stable, positional validation failure."""
        violations.append(Violation(code=code, message=message, position=position, field=field))

    if [track.position for track in parsed_proposal.tracks] != list(range(1, 11)):
        add_violation("invalid_positions", "Track positions must be 1 through 10 in order.")
    if sum(track.wildcard for track in parsed_proposal.tracks) != 5:
        add_violation("invalid_split", "Select exactly five catalog tracks and five wildcards.")

    seen_track_ids: set[str] = set()
    seen_primary_artists: set[str] = set()
    for track in parsed_proposal.tracks:
        if track.spotify_id in seen_track_ids:
            add_violation("duplicate_track", "This track is already selected.", track.position)
        seen_track_ids.add(track.spotify_id)
        primary_artist = normalize_artist(track.artists[0])
        if primary_artist in seen_primary_artists:
            add_violation(
                "duplicate_artist", "This primary artist is already selected.", track.position
            )
        seen_primary_artists.add(primary_artist)
        if track.spotify_id in context.recent_track_ids:
            add_violation(
                "recently_played",
                "This track was published in the previous 30 days.",
                track.position,
            )

        catalog_track = context.catalog.get(track.spotify_id)
        search_track = context.search_results.get(track.spotify_id)
        if track.wildcard:
            if catalog_track is not None:
                add_violation(
                    "wildcard_in_catalog",
                    "Wildcards must be outside the fixed shared catalog.",
                    track.position,
                )
            if search_track is None:
                add_violation(
                    "unverified_wildcard",
                    "Search Spotify and select a returned recording before using this wildcard.",
                    track.position,
                )
                canonical_metadata = None
            else:
                canonical_metadata = {
                    "spotify_id": search_track.id,
                    "title": search_track.name,
                    "artists": search_track.artists,
                    "duration_ms": search_track.duration_ms,
                }
        elif catalog_track is None:
            add_violation(
                "unknown_catalog_track",
                "This recording is absent from the catalog.",
                track.position,
            )
            canonical_metadata = None
        else:
            canonical_metadata = {
                "spotify_id": catalog_track.spotify_id,
                "title": catalog_track.title,
                "artists": catalog_track.artists,
                "duration_ms": catalog_track.duration_ms,
            }

        if track.spotify_url != f"https://open.spotify.com/track/{track.spotify_id}":
            add_violation(
                "metadata_mismatch",
                "Spotify URL must be the canonical URL for the selected track ID.",
                track.position,
                "spotify_url",
            )
        if canonical_metadata is not None:
            for field_name, canonical_value in canonical_metadata.items():
                if getattr(track, field_name) != canonical_value:
                    add_violation(
                        "metadata_mismatch",
                        f"Use the canonical {field_name} returned for this recording.",
                        track.position,
                        field_name,
                    )

    readings_by_key = {reading.key: reading for reading in context.readings}
    effect_keys: set[str] = set()
    for effect in parsed_proposal.effects:
        if effect.key in effect_keys:
            add_violation("duplicate_effect", f"Input {effect.key} has more than one effect.")
        effect_keys.add(effect.key)
        reading = readings_by_key.get(effect.key)
        if reading is None:
            add_violation("unknown_effect", f"Input {effect.key} is not part of this run.")
            continue
        if reading.status == "skipped" or (
            reading.status == "stale" and not _has_usable_data(reading)
        ):
            if effect.effect.strip() or effect.weight not in (None, 0):
                add_violation(
                    "unavailable_input_effect",
                    f"Input {effect.key} has no usable evidence; "
                    "leave its effect empty and weight null or zero.",
                )
        if effect.key == "dj_roll" and effect.weight is not None:
            add_violation("dj_roll_weight", "The DJ roll must have a null weight.")
    for missing_key in sorted(readings_by_key.keys() - effect_keys):
        add_violation("missing_effect", f"Include one effect entry for input {missing_key}.")

    return ValidationResult(valid=not violations, violations=violations)


def assemble_daily_record(
    proposal: PlaylistProposal,
    context: ValidationContext,
    prompt: str,
    model: str,
    generated_at: datetime,
) -> DailyRecord:
    """Validate and attach trusted evidence and publication metadata to a proposal."""
    validation_result = _validate_playlist(proposal, context)
    if not validation_result.valid:
        raise ValueError("; ".join(violation.message for violation in validation_result.violations))
    effects_by_key = {effect.key: effect for effect in proposal.effects}
    return DailyRecord(
        date=context.run_date,
        dj=context.dj_id,
        title=proposal.title,
        description=proposal.description,
        liner_notes=proposal.liner_notes,
        tracks=[
            DailyTrack(**track.model_dump(), artist=", ".join(track.artists))
            for track in proposal.tracks
        ],
        inputs=[
            DailyInput(
                **reading.model_dump(),
                effect=effects_by_key[reading.key].effect,
                weight=effects_by_key[reading.key].weight,
            )
            for reading in context.readings
        ],
        mood=context.mood,
        publication_status="pending",
        spotify_playlist_id=None,
        prompt=prompt,
        model=model,
        generated_at=generated_at,
    )
