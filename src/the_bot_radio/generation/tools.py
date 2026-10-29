"""In-process SDK tools for verified Spotify search and playlist feedback."""

import asyncio
import json
from typing import Annotated, Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from pydantic import Field, StringConstraints, ValidationError

from the_bot_radio.generation.models import (
    GenerationModel,
    PlaylistProposal,
    ValidationResult,
    Violation,
)
from the_bot_radio.generation.runtime import EventSink
from the_bot_radio.generation.validation import PlaylistValidator
from the_bot_radio.services.reccobeats_service import ReccobeatsService, RecommendationRequest
from the_bot_radio.services.spotify_service import SpotifyService


class SearchArguments(GenerationModel):
    """Bound the search workload and reject unsupported agent arguments."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    limit: int = Field(default=10, ge=1, le=10)
    offset: int = Field(default=0, ge=0, le=1000)


class AudioFeaturesArguments(GenerationModel):
    """Bound feature lookups to one provider batch without accepting host state."""

    ids: list[Annotated[str, StringConstraints(min_length=1, pattern=r"^[^,]+$")]] = Field(
        min_length=1, max_length=40
    )


class RecommendationArguments(RecommendationRequest):
    """Retain provider controls while bounding the agent's research workload."""

    size: int = Field(default=10, ge=1, le=20, strict=True)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """Serialize a predictable JSON response in MCP's text content envelope."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": is_error,
    }


class PlaylistTools:
    """Hold trusted references and serialize Spotify requests for a single run."""

    def __init__(
        self,
        validator: PlaylistValidator,
        spotify_service: SpotifyService,
        event_sink: EventSink | None = None,
        *,
        reccobeats_service: ReccobeatsService,
    ) -> None:
        """Capture host-owned state instead of exposing it as tool arguments."""
        self.validator = validator
        self.spotify_service = spotify_service
        self.reccobeats_service = reccobeats_service
        self.event_sink = event_sink
        self.search_lock = asyncio.Lock()

    def _emit(self, event: dict[str, Any]) -> None:
        """Forward sanitized tool outcomes to the host's transcript writer."""
        if self.event_sink is not None:
            self.event_sink(event)

    async def search_tracks(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Search Spotify and cache only canonical successful results for validation."""
        try:
            search_arguments = SearchArguments.model_validate(arguments)
        except ValidationError:
            return _tool_result({"error": "invalid_search_arguments"}, is_error=True)

        try:
            async with self.search_lock:
                tracks = await self.spotify_service.search_tracks(
                    search_arguments.query,
                    limit=search_arguments.limit,
                    offset=search_arguments.offset,
                )
                canonical_tracks = [
                    track.model_copy(
                        update={"external_url": f"https://open.spotify.com/track/{track.id}"}
                    )
                    for track in tracks
                ]
                self.validator.context.search_results.update(
                    {track.id: track for track in canonical_tracks}
                )
        except Exception:
            # Provider errors may contain token-bearing URLs or credentials; never echo them.
            self._emit({"event": "spotify_search", "status": "error"})
            return _tool_result({"error": "spotify_search_failed"}, is_error=True)

        payload = {
            "tracks": [
                {
                    "spotify_id": track.id,
                    "spotify_url": track.external_url,
                    "title": track.name,
                    "artists": track.artists,
                    "album": track.album,
                    "duration_ms": track.duration_ms,
                }
                for track in canonical_tracks
            ]
        }
        self._emit({"event": "spotify_search", "status": "ok", "track_ids": [t.id for t in tracks]})
        return _tool_result(payload)

    async def get_audio_features(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return optional provider features without establishing selection evidence."""
        event_name = "reccobeats_audio_features"
        try:
            feature_arguments = AudioFeaturesArguments.model_validate(arguments)
        except ValidationError:
            self._emit({"event": event_name, "status": "invalid_arguments"})
            return _tool_result({"error": "invalid_audio_features_arguments"}, is_error=True)
        try:
            envelope = await self.reccobeats_service.get_audio_features(feature_arguments.ids)
            row_count = len(envelope["content"])
        except Exception:
            self._emit({"event": event_name, "status": "error"})
            return _tool_result({"error": "audio_features_failed"}, is_error=True)
        self._emit(
            {"event": event_name, "status": "ok" if row_count else "empty", "row_count": row_count}
        )
        return _tool_result(envelope)

    async def get_recommendation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return optional discovery metadata without changing canonical search evidence."""
        event_name = "reccobeats_recommendation"
        try:
            recommendation_arguments = RecommendationArguments.model_validate(arguments)
        except ValidationError:
            self._emit({"event": event_name, "status": "invalid_arguments"})
            return _tool_result({"error": "invalid_recommendation_arguments"}, is_error=True)
        try:
            envelope = await self.reccobeats_service.get_recommendation(recommendation_arguments)
            row_count = len(envelope["content"])
        except Exception:
            self._emit({"event": event_name, "status": "error"})
            return _tool_result({"error": "recommendation_failed"}, is_error=True)
        self._emit(
            {"event": event_name, "status": "ok" if row_count else "empty", "row_count": row_count}
        )
        return _tool_result(envelope)

    async def validate_playlist(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return actionable violations using fixed run evidence and search results."""
        if set(arguments) != {"proposal"}:
            result = ValidationResult(
                valid=False,
                violations=[
                    Violation(code="invalid_arguments", message="Provide only the proposal field.")
                ],
            )
        else:
            result = self.validator.validate_playlist(arguments["proposal"])
        payload = result.model_dump(mode="json")
        self._emit({"event": "playlist_validation", **payload})
        return _tool_result(payload)


def build_tool_server(
    validator: PlaylistValidator,
    spotify_service: SpotifyService,
    event_sink: EventSink | None = None,
    *,
    reccobeats_service: ReccobeatsService,
) -> McpSdkServerConfig:
    """Register the four callable tools in an SDK MCP server within the host process."""
    adapters = PlaylistTools(
        validator, spotify_service, event_sink, reccobeats_service=reccobeats_service
    )
    search_tool = tool(
        "search_tracks",
        "Search Spotify recordings by query, with optional limit (default 10, maximum 10) and "
        "offset. Returns tracks with Spotify IDs, URLs, titles, artists, albums, and durations. "
        "Successful results are retained as canonical evidence for playlist validation. "
        "Invalid arguments or provider failures return an error with isError=true.",
        SearchArguments.model_json_schema(),
    )(adapters.search_tracks)
    proposal_schema = PlaylistProposal.model_json_schema()
    definitions = proposal_schema.pop("$defs", {})
    validation_schema = {
        "type": "object",
        "properties": {"proposal": proposal_schema},
        "required": ["proposal"],
        "additionalProperties": False,
        "$defs": definitions,
    }
    validation_tool = tool(
        "validate_playlist",
        "Validate a proposal against playlist rules, the fixed catalog, recent published history, "
        "and retained Spotify search results. Accepts only the proposal field. Returns valid and "
        "a list of violations with codes, messages, and affected positions or fields "
        "where applicable.",
        validation_schema,
    )(adapters.validate_playlist)
    features_tool = tool(
        "get_audio_features",
        "Look up ReccoBeats audio features for ids: 1–40 nonempty track IDs without commas. "
        "Returns acousticness, danceability, energy, instrumentalness, key, liveness, loudness, "
        "mode, speechiness, tempo, and valence for covered tracks.",
        AudioFeaturesArguments.model_json_schema(),
    )(adapters.get_audio_features)
    recommendation_tool = tool(
        "get_recommendation",
        "Request ReccoBeats recommendations from 1–5 seed track IDs. "
        "Optional controls target acousticness, danceability, energy, instrumentalness, liveness, "
        "speechiness, valence, key, mode, loudness, tempo, popularity, and featureWeight. "
        "Returns the provider envelope with track metadata, not audio features. "
        "Does not update the catalog or validation evidence. "
        "Invalid arguments or provider failures return an error with isError=true.",
        RecommendationArguments.model_json_schema(),
    )(adapters.get_recommendation)
    return create_sdk_mcp_server(
        name="playlist", tools=[search_tool, validation_tool, features_tool, recommendation_tool]
    )
