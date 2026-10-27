"""Tool adapters expose canonical evidence and deterministic feedback without publishing."""

import asyncio
import json
from datetime import date
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from the_bot_radio.generation import tools
from the_bot_radio.generation.models import PlaylistMood, ValidationContext
from the_bot_radio.generation.tools import PlaylistTools, build_tool_server
from the_bot_radio.generation.validation import PlaylistValidator
from the_bot_radio.services.spotify_service import SpotifyError, SpotifyService, SpotifyTrack

pytestmark = pytest.mark.anyio


def context() -> ValidationContext:
    """Build independent reference state for one adapter run."""
    return ValidationContext(
        run_date=date(2026, 10, 27),
        dj_id="resident-01",
        catalog={},
        recent_track_ids=frozenset(),
        readings=(),
        mood=PlaylistMood(tempo_bpm=90, energy=4, brightness=3, obscurity=7),
    )


def track(identifier: str = "a" * 22, title: str = "A recording") -> SpotifyTrack:
    """Return metadata that has not yet acquired a canonical external URL."""
    return SpotifyTrack(
        id=identifier,
        uri=f"spotify:track:{identifier}",
        name=title,
        artists=["An artist"],
        album="An album",
        duration_ms=180000,
        explicit=False,
    )


def payload(result: dict[str, Any]) -> dict[str, Any]:
    """Decode an MCP text result for assertions."""
    return json.loads(result["content"][0]["text"])


async def test_search_retains_canonical_versions_and_emits_sanitized_event() -> None:
    spotify_service = AsyncMock(spec=SpotifyService)
    spotify_service.search_tracks.return_value = [track(), track("b" * 22, "A recording - Live")]
    run_context = context()
    events: list[dict[str, Any]] = []
    adapters = PlaylistTools(PlaylistValidator(run_context), spotify_service, events.append)

    result = await adapters.search_tracks({"query": 'track:"A recording" artist:"An artist"'})

    assert not result["isError"]
    returned_tracks = payload(result)["tracks"]
    assert [item["title"] for item in returned_tracks] == ["A recording", "A recording - Live"]
    assert returned_tracks[0]["spotify_url"] == f"https://open.spotify.com/track/{'a' * 22}"
    assert run_context.search_results["a" * 22].external_url == returned_tracks[0]["spotify_url"]
    spotify_service.search_tracks.assert_awaited_once_with(
        'track:"A recording" artist:"An artist"', limit=10, offset=0
    )
    assert events == [
        {"event": "spotify_search", "status": "ok", "track_ids": list(run_context.search_results)}
    ]
    spotify_service.create_playlist.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " "},
        {"query": "song", "limit": 11},
        {"query": "song", "offset": -1},
        {"query": "song", "offset": 1001},
        {"query": "song", "catalog": {}},
    ],
)
async def test_search_rejects_unbounded_or_untrusted_arguments(arguments: dict[str, Any]) -> None:
    spotify_service = AsyncMock(spec=SpotifyService)
    result = await PlaylistTools(PlaylistValidator(context()), spotify_service).search_tracks(
        arguments
    )
    assert result["isError"]
    spotify_service.search_tracks.assert_not_called()


async def test_empty_and_failed_searches_do_not_invent_cached_evidence() -> None:
    spotify_service = AsyncMock(spec=SpotifyService)
    spotify_service.search_tracks.side_effect = [
        [],
        SpotifyError(401, "https://secret.example?token=do-not-leak"),
    ]
    run_context = context()
    events: list[dict[str, Any]] = []
    adapters = PlaylistTools(PlaylistValidator(run_context), spotify_service, events.append)

    assert payload(await adapters.search_tracks({"query": "unknown"})) == {"tracks": []}
    failed = await adapters.search_tracks({"query": "another"})
    assert failed["isError"]
    assert payload(failed) == {"error": "spotify_search_failed"}
    assert run_context.search_results == {}
    assert "do-not-leak" not in json.dumps([failed, events])


async def test_search_requests_are_serialized_and_run_caches_are_independent() -> None:
    active_calls = 0
    maximum_active_calls = 0

    async def search(*args: Any, **kwargs: Any) -> list[SpotifyTrack]:
        nonlocal active_calls, maximum_active_calls
        active_calls += 1
        maximum_active_calls = max(active_calls, maximum_active_calls)
        await asyncio.sleep(0)
        active_calls -= 1
        return [track()]

    spotify_service = AsyncMock(spec=SpotifyService)
    spotify_service.search_tracks.side_effect = search
    first_context, second_context = context(), context()
    adapters = PlaylistTools(PlaylistValidator(first_context), spotify_service)
    await asyncio.gather(
        adapters.search_tracks({"query": "one"}), adapters.search_tracks({"query": "two"})
    )
    assert maximum_active_calls == 1
    assert first_context.search_results
    assert second_context.search_results == {}


async def test_validation_returns_structured_feedback_and_rejects_reference_override() -> None:
    spotify_service = AsyncMock(spec=SpotifyService)
    events: list[dict[str, Any]] = []
    adapters = PlaylistTools(PlaylistValidator(context()), spotify_service, events.append)

    invalid_proposal = payload(await adapters.validate_playlist({"proposal": {}}))
    assert not invalid_proposal["valid"]
    assert invalid_proposal["violations"]
    assert events[-1]["event"] == "playlist_validation"

    attempted_override = payload(
        await adapters.validate_playlist({"proposal": {}, "context": {"recent_track_ids": []}})
    )
    assert attempted_override["violations"][0]["code"] == "invalid_arguments"
    spotify_service.search_tracks.assert_not_called()


def test_tool_registration_keeps_nested_schema_definitions_at_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create_server(**arguments: Any) -> dict[str, Any]:
        captured.update(arguments)
        return {"type": "sdk", "name": arguments["name"], "instance": object()}

    monkeypatch.setattr(tools, "create_sdk_mcp_server", create_server)
    server = build_tool_server(
        PlaylistValidator(context()), cast(SpotifyService, AsyncMock(spec=SpotifyService))
    )
    assert server["name"] == "playlist"
    assert [registered.name for registered in captured["tools"]] == [
        "search_tracks",
        "validate_playlist",
    ]
    validation_schema = captured["tools"][1].input_schema
    assert validation_schema["required"] == ["proposal"]
    assert "PlaylistTrack" in validation_schema["$defs"]
    assert "$defs" not in validation_schema["properties"]["proposal"]
