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
from the_bot_radio.services.spotify_service import SpotifyService


class SearchArguments(GenerationModel):
    """Bound the search workload and reject unsupported agent arguments."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    limit: int = Field(default=10, ge=1, le=10)
    offset: int = Field(default=0, ge=0, le=1000)


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
    ) -> None:
        """Capture host-owned state instead of exposing it as tool arguments."""
        self.validator = validator
        self.spotify_service = spotify_service
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
) -> McpSdkServerConfig:
    """Register the two callable tools in an SDK MCP server within the host process."""
    adapters = PlaylistTools(validator, spotify_service, event_sink)
    search_tool = tool(
        "search_tracks",
        "Search Spotify for recordings; select the correct title, artists, and version. "
        "Results provide canonical metadata and are retained for playlist validation.",
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
        "Check the proposed playlist against the catalog, recent published history, "
        "and verified search results. Fix violations and validate again before finishing.",
        validation_schema,
    )(adapters.validate_playlist)
    return create_sdk_mcp_server(name="playlist", tools=[search_tool, validation_tool])
