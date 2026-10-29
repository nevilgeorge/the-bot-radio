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
from the_bot_radio.services.reccobeats_service import ReccobeatsService, RecommendationRequest
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
    adapters = PlaylistTools(
        PlaylistValidator(run_context),
        spotify_service,
        events.append,
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    )

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
    result = await PlaylistTools(
        PlaylistValidator(context()),
        spotify_service,
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    ).search_tracks(arguments)
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
    adapters = PlaylistTools(
        PlaylistValidator(run_context),
        spotify_service,
        events.append,
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    )

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
    adapters = PlaylistTools(
        PlaylistValidator(first_context),
        spotify_service,
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    )
    await asyncio.gather(
        adapters.search_tracks({"query": "one"}), adapters.search_tracks({"query": "two"})
    )
    assert maximum_active_calls == 1
    assert first_context.search_results
    assert second_context.search_results == {}


async def test_validation_returns_structured_feedback_and_rejects_reference_override() -> None:
    spotify_service = AsyncMock(spec=SpotifyService)
    events: list[dict[str, Any]] = []
    adapters = PlaylistTools(
        PlaylistValidator(context()),
        spotify_service,
        events.append,
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    )

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
        PlaylistValidator(context()),
        cast(SpotifyService, AsyncMock(spec=SpotifyService)),
        reccobeats_service=AsyncMock(spec=ReccobeatsService),
    )
    assert server["name"] == "playlist"
    assert [registered.name for registered in captured["tools"]] == [
        "search_tracks",
        "validate_playlist",
        "get_audio_features",
        "get_recommendation",
    ]
    assert captured["tools"][2].input_schema == tools.AudioFeaturesArguments.model_json_schema()
    assert captured["tools"][3].input_schema == tools.RecommendationArguments.model_json_schema()
    assert captured["tools"][2].input_schema == tools.AudioFeaturesArguments.model_json_schema()
    assert captured["tools"][3].input_schema == tools.RecommendationArguments.model_json_schema()
    validation_schema = captured["tools"][1].input_schema
    assert validation_schema["required"] == ["proposal"]
    assert "PlaylistTrack" in validation_schema["$defs"]
    assert "$defs" not in validation_schema["properties"]["proposal"]


def research_adapters(
    reccobeats_service: ReccobeatsService, events: list[dict[str, Any]]
) -> PlaylistTools:
    """Build research tools with isolated validator evidence and a fake provider."""
    return PlaylistTools(
        PlaylistValidator(context()),
        AsyncMock(spec=SpotifyService),
        events.append,
        reccobeats_service=reccobeats_service,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"ids": []},
        {"ids": ["track"] * 41},
        {"ids": [""]},
        {"ids": ["first,second"]},
        {"ids": [12]},
        {"ids": "track"},
        {"ids": ["track"], "other": True},
    ],
)
async def test_audio_features_reject_invalid_arguments(arguments: dict[str, Any]) -> None:
    service = AsyncMock(spec=ReccobeatsService)
    events: list[dict[str, Any]] = []
    result = await research_adapters(service, events).get_audio_features(arguments)
    assert result["isError"]
    assert payload(result) == {"error": "invalid_audio_features_arguments"}
    assert events == [{"event": "reccobeats_audio_features", "status": "invalid_arguments"}]
    service.get_audio_features.assert_not_called()


@pytest.mark.parametrize("requested_count", [1, 2, 40])
@pytest.mark.parametrize("rows", [[], [{"id": "provider-id", "energy": 0.7}]])
async def test_audio_features_preserve_partial_and_empty_envelopes(
    requested_count: int, rows: list[dict[str, Any]]
) -> None:
    service = AsyncMock(spec=ReccobeatsService)
    envelope = {"content": rows, "metadata": {"provider": "retained"}}
    service.get_audio_features.return_value = envelope
    events: list[dict[str, Any]] = []
    adapters = research_adapters(service, events)
    ids = ["spotify-id", "reccobeats-id"][:requested_count]
    if requested_count == 40:
        ids = [f"id-{index}" for index in range(40)]
    result = await adapters.get_audio_features({"ids": ids})
    assert not result["isError"]
    assert payload(result) == envelope
    service.get_audio_features.assert_awaited_once_with(ids)
    assert events == [
        {
            "event": "reccobeats_audio_features",
            "status": "ok" if rows else "empty",
            "row_count": len(rows),
        }
    ]
    assert adapters.validator.context.search_results == {}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"seeds": []},
        {"seeds": ["seed"] * 6},
        {"seeds": [""]},
        {"seeds": ["first,second"]},
        {"seeds": ["two words"]},
        {"seeds": ["seed"], "size": 0},
        {"seeds": ["seed"], "size": 21},
        {"seeds": ["seed"], "size": True},
        {"seeds": ["seed"], "size": 1.0},
        {"seeds": ["seed"], "size": "10"},
        {"seeds": ["seed"], "negativeSeeds": []},
        {"seeds": ["seed"], "unknown": 1},
        {"seeds": ["seed"], "energy": 1.1},
        {"seeds": ["seed"], "energy": "0.5"},
        {"seeds": ["seed"], "tempo": 251.0},
        {"seeds": ["seed"], "key": 12},
        {"seeds": ["seed"], "mode": 2},
        {"seeds": ["seed"], "popularity": 101},
        {"seeds": ["seed"], "featureWeight": 0.0},
    ],
)
async def test_recommendations_reject_invalid_arguments(arguments: dict[str, Any]) -> None:
    service = AsyncMock(spec=ReccobeatsService)
    events: list[dict[str, Any]] = []
    result = await research_adapters(service, events).get_recommendation(arguments)
    assert result["isError"]
    assert payload(result) == {"error": "invalid_recommendation_arguments"}
    assert events == [{"event": "reccobeats_recommendation", "status": "invalid_arguments"}]
    service.get_recommendation.assert_not_called()


@pytest.mark.parametrize("size", [None, 1, 20])
@pytest.mark.parametrize("weight_field", ["feature_weight", "featureWeight"])
@pytest.mark.parametrize("rows", [[], [{"id": "provider-id", "trackTitle": "Unchanged"}]])
async def test_recommendations_delegate_controls_and_preserve_envelopes(
    size: int | None, weight_field: str, rows: list[dict[str, Any]]
) -> None:
    service = AsyncMock(spec=ReccobeatsService)
    envelope = {"content": rows, "total": len(rows), "extra": "unchanged"}
    service.get_recommendation.return_value = envelope
    events: list[dict[str, Any]] = []
    adapters = research_adapters(service, events)
    arguments: dict[str, Any] = {
        "seeds": ["spotify-id", "reccobeats-id"],
        "energy": 0.5,
        "tempo": 120.0,
        "key": -1,
        "mode": 0,
        "popularity": 50,
        weight_field: 2.0,
    }
    if size is not None:
        arguments["size"] = size
    result = await adapters.get_recommendation(arguments)
    assert not result["isError"]
    assert payload(result) == envelope
    expected = RecommendationRequest.model_validate(arguments)
    request = service.get_recommendation.await_args.args[0]
    assert request.model_dump() == expected.model_dump()
    assert request.size == (10 if size is None else size)
    service.get_recommendation.assert_awaited_once_with(request)
    assert events == [
        {
            "event": "reccobeats_recommendation",
            "status": "ok" if rows else "empty",
            "row_count": len(rows),
        }
    ]
    assert adapters.validator.context.search_results == {}


@pytest.mark.parametrize(
    ("method", "arguments", "error_code", "event_name"),
    [
        (
            "get_audio_features",
            {"ids": ["seed"]},
            "audio_features_failed",
            "reccobeats_audio_features",
        ),
        (
            "get_recommendation",
            {"seeds": ["seed"]},
            "recommendation_failed",
            "reccobeats_recommendation",
        ),
    ],
)
async def test_research_failures_are_sanitized_and_cancellation_propagates(
    method: str, arguments: dict[str, Any], error_code: str, event_name: str
) -> None:
    service = AsyncMock(spec=ReccobeatsService)
    provider_method = getattr(service, method)
    provider_method.side_effect = RuntimeError("https://secret.example?token=do-not-leak")
    events: list[dict[str, Any]] = []
    adapter_method = getattr(research_adapters(service, events), method)
    result = await adapter_method(arguments)
    assert result["isError"]
    assert payload(result) == {"error": error_code}
    assert events == [{"event": event_name, "status": "error"}]
    assert "do-not-leak" not in json.dumps([result, events])
    provider_method.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await adapter_method(arguments)
    assert events == [{"event": event_name, "status": "error"}]


def test_research_schemas_match_runtime_boundaries() -> None:
    """Generated schemas publish provider controls and agent-specific limits."""
    feature_schema = tools.AudioFeaturesArguments.model_json_schema()
    assert feature_schema["additionalProperties"] is False
    ids_schema = feature_schema["properties"]["ids"]
    assert (ids_schema["minItems"], ids_schema["maxItems"]) == (1, 40)
    assert ids_schema["items"]["minLength"] == 1
    assert ids_schema["items"]["pattern"] == r"^[^,]+$"
    recommendation_schema = tools.RecommendationArguments.model_json_schema()
    assert recommendation_schema["additionalProperties"] is False
    assert "negativeSeeds" not in recommendation_schema["properties"]
    assert recommendation_schema["properties"]["size"] == {
        "default": 10,
        "minimum": 1,
        "maximum": 20,
        "title": "Size",
        "type": "integer",
    }
    assert "featureWeight" in recommendation_schema["properties"]
    assert RecommendationRequest(seeds=("seed",), size=100).size == 100
