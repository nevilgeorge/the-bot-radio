"""Offline transport, pacing and request-contract coverage for ReccoBeats."""

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
import respx
from pydantic import ValidationError

from the_bot_radio.services import reccobeats_service as module
from the_bot_radio.services.reccobeats_service import (
    API_BASE,
    ReccobeatsError,
    ReccobeatsService,
    RecommendationRequest,
)

pytestmark = pytest.mark.anyio
FIXTURES = Path(__file__).parent / "fixtures" / "reccobeats"
TRACK_ID = "00aqkszH1FdUiJJWvX6iEl"


def payload(name: str) -> dict[str, Any]:
    """Load a fresh captured provider envelope."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@respx.mock
async def test_endpoints_preserve_envelopes_and_query_encoding() -> None:
    audio_payload = payload("audio_features")
    audio_payload["extra"] = {"preserved": True}
    audio_route = respx.get(API_BASE + "/audio-features").respond(200, json=audio_payload)
    recommendation_payload = payload("recommendation")
    recommendation_route = respx.get(API_BASE + "/track/recommendation").respond(
        200, json=recommendation_payload
    )
    async with httpx.AsyncClient() as http_client:
        service = ReccobeatsService(http_client, request_interval=0)
        assert await service.get_audio_features([TRACK_ID, "another"]) == audio_payload
        assert (
            await service.get_recommendation(
                RecommendationRequest(seeds=[TRACK_ID, "another"], feature_weight=2.5, energy=0)
            )
            == recommendation_payload
        )
        assert not http_client.is_closed
    assert audio_route.calls.last.request.url.params["ids"] == f"{TRACK_ID},another"
    params = recommendation_route.calls.last.request.url.params
    assert dict(params) == {
        "seeds": f"{TRACK_ID},another",
        "size": "10",
        "featureWeight": "2.5",
        "energy": "0.0",
    }
    assert "negativeSeeds" not in recommendation_route.calls.last.request.url.params
    assert "trackTitle" in recommendation_payload["content"][0]
    assert "energy" not in recommendation_payload["content"][0]


@pytest.mark.parametrize("values", [[], ["x"] * 6, [""], ["x,y"], [" "]])
def test_invalid_seeds(values: list[str]) -> None:
    with pytest.raises(ValidationError):
        RecommendationRequest(seeds=values)


@pytest.mark.parametrize(
    ("control", "valid_bounds", "invalid_values"),
    [
        (name, (0, 1), (-0.1, 1.1, float("inf"), float("nan")))
        for name in (
            "acousticness",
            "danceability",
            "energy",
            "instrumentalness",
            "liveness",
            "speechiness",
            "valence",
        )
    ]
    + [
        ("key", (-1, 11), (-2, 12, 1.5)),
        ("mode", (0, 1), (-1, 2, 0.5)),
        ("loudness", (-60, 2), (-61, 3, float("nan"))),
        ("tempo", (0, 250), (-1, 251, float("inf"))),
        ("popularity", (0, 100), (-1, 101, 1.5)),
        ("feature_weight", (1, 5), (0, 6, float("nan"))),
        ("size", (1, 100), (0, 101, 1.5)),
    ],
)
def test_control_bounds(
    control: str, valid_bounds: tuple[int, int], invalid_values: tuple[float, ...]
) -> None:
    for value in valid_bounds:
        RecommendationRequest(seeds=[TRACK_ID], **{control: value})
    for value in (*invalid_values, True, "1"):
        with pytest.raises(ValidationError):
            RecommendationRequest(seeds=[TRACK_ID], **{control: value})


@pytest.mark.parametrize("unknown", ["negativeSeeds", "negative_seeds", "unknown"])
def test_unknown_controls_forbidden(unknown: str) -> None:
    with pytest.raises(ValidationError):
        RecommendationRequest(seeds=[TRACK_ID], **{unknown: []})


def test_request_is_immutable() -> None:
    request = RecommendationRequest(seeds=[TRACK_ID])
    assert request.seeds == (TRACK_ID,)
    with pytest.raises(ValidationError):
        request.size = 101


@respx.mock
async def test_audio_id_limits_and_empty_no_http() -> None:
    route = respx.get(API_BASE + "/audio-features").respond(200, json={"content": []})
    async with httpx.AsyncClient() as http_client:
        service = ReccobeatsService(http_client, request_interval=0)
        assert await service.get_audio_features([]) == {"content": []}
        assert not route.called
        for ids in (["x"] * 41, [""], ["x,y"], "string"):
            with pytest.raises(ValueError):
                await service.get_audio_features(ids)
        assert not route.called
        await service.get_audio_features(["x"] * 40)
        assert route.call_count == 1


@pytest.mark.parametrize("malformed", [[], None, {}, {"content": None}, {"content": {}}])
@respx.mock
async def test_malformed_envelopes(malformed: Any) -> None:
    respx.get(API_BASE + "/audio-features").respond(200, json=malformed)
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ReccobeatsError):
            await ReccobeatsService(http_client).get_audio_features([TRACK_ID])


@respx.mock
async def test_invalid_json_sanitized() -> None:
    respx.get(API_BASE + "/audio-features").respond(200, text="private body https://secret")
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ReccobeatsError, match="^reccobeats returned invalid JSON$"):
            await ReccobeatsService(http_client).get_audio_features([TRACK_ID])


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
async def test_endpoint_specific_http_errors(status: int) -> None:
    audio = respx.get(API_BASE + "/audio-features").respond(status, text="secret body")
    recommendation = respx.get(API_BASE + "/track/recommendation").respond(status)
    async with httpx.AsyncClient() as http_client:
        service = ReccobeatsService(http_client, request_interval=0)
        if status == 404:
            assert await service.get_audio_features([TRACK_ID]) == {"content": []}
        else:
            with pytest.raises(ReccobeatsError) as caught:
                await service.get_audio_features([TRACK_ID])
            assert caught.value.credential is (status in (401, 403))
            assert "secret" not in str(caught.value)
            assert "https" not in str(caught.value)
        with pytest.raises(ReccobeatsError):
            await service.get_recommendation(RecommendationRequest(seeds=[TRACK_ID]))
    assert audio.call_count == recommendation.call_count == 1


class FakeClock:
    """Advance monotonic and wall time without real retry sleeps."""

    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        """Record each wait and advance time by that delay."""
        self.sleeps.append(delay)
        self.value += delay


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch the service clock and sleeps for deterministic pacing tests."""
    clock = FakeClock()
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.value)
    monkeypatch.setattr(module.time, "time", lambda: 1000 + clock.value)
    monkeypatch.setattr(module.anyio, "sleep", clock.sleep)
    return clock


@pytest.mark.parametrize("failure", [429, 500, "transport"])
@respx.mock
async def test_three_attempts_backoff_and_cross_call_cooldown(
    failure: int | str, fake_clock: FakeClock
) -> None:
    route = respx.get(API_BASE + "/audio-features")
    if failure == "transport":
        route.mock(side_effect=httpx.ConnectError("https://private-token"))
    else:
        route.respond(failure, text="private-token")
    recommendation = respx.get(API_BASE + "/track/recommendation").respond(
        200, json={"content": []}
    )
    async with httpx.AsyncClient() as http_client:
        service = ReccobeatsService(http_client)
        with pytest.raises(ReccobeatsError) as caught:
            await service.get_audio_features([TRACK_ID])
        assert "private-token" not in str(caught.value)
        assert route.call_count == 3
        await service.get_recommendation(RecommendationRequest(seeds=[TRACK_ID]))
    assert recommendation.call_count == 1
    assert fake_clock.sleeps == [0, 1, 2, 4]


@pytest.mark.parametrize("http_date", [False, True])
@respx.mock
async def test_retry_after_forms(http_date: bool, fake_clock: FakeClock) -> None:
    retry_after = (
        format_datetime(datetime.fromtimestamp(1107, UTC), usegmt=True) if http_date else "7"
    )
    respx.get(API_BASE + "/audio-features").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": retry_after}),
            httpx.Response(200, json={"content": []}),
        ]
    )
    async with httpx.AsyncClient() as http_client:
        await ReccobeatsService(http_client).get_audio_features([TRACK_ID])
    assert fake_clock.sleeps == [0, 7]


@respx.mock
async def test_both_endpoints_share_pacing(fake_clock: FakeClock) -> None:
    respx.get(API_BASE + "/audio-features").respond(200, json={"content": []})
    respx.get(API_BASE + "/track/recommendation").respond(200, json={"content": []})
    async with httpx.AsyncClient() as http_client:
        service = ReccobeatsService(http_client)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(service.get_audio_features, [TRACK_ID])
            tasks.start_soon(service.get_recommendation, RecommendationRequest(seeds=[TRACK_ID]))
    assert fake_clock.sleeps == [0, 1]


async def test_cancellation_propagates_and_releases_lock() -> None:
    async with httpx.AsyncClient() as http_client:
        http_client.get = AsyncMock(side_effect=anyio.get_cancelled_exc_class())
        service = ReccobeatsService(http_client, request_interval=0)
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await service.get_audio_features([TRACK_ID])
        http_client.get = AsyncMock(return_value=httpx.Response(200, json={"content": []}))
        assert await service.get_audio_features([TRACK_ID]) == {"content": []}
