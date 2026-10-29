"""Shared, paced ReccoBeats HTTP lookups without catalog or client lifecycle ownership."""

import math
import time
from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from typing import Annotated, Any

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, Field

API_BASE = "https://api.reccobeats.com/v1"
NormalizedFeature = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class RecommendationRequest(BaseModel):
    """Validated recommendation seeds and optional provider feature targets."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    seeds: tuple[Annotated[str, Field(min_length=1, pattern=r"^[^,\s]+$")], ...] = Field(
        min_length=1, max_length=5
    )
    size: int = Field(default=10, ge=1, le=100, strict=True)
    acousticness: NormalizedFeature | None = None
    danceability: NormalizedFeature | None = None
    energy: NormalizedFeature | None = None
    instrumentalness: NormalizedFeature | None = None
    liveness: NormalizedFeature | None = None
    speechiness: NormalizedFeature | None = None
    valence: NormalizedFeature | None = None
    key: int | None = Field(default=None, ge=-1, le=11, strict=True)
    mode: int | None = Field(default=None, ge=0, le=1, strict=True)
    loudness: float | None = Field(default=None, ge=-60, le=2, allow_inf_nan=False, strict=True)
    tempo: float | None = Field(default=None, ge=0, le=250, allow_inf_nan=False, strict=True)
    popularity: int | None = Field(default=None, ge=0, le=100, strict=True)
    feature_weight: float | None = Field(
        default=None, alias="featureWeight", ge=1, le=5, allow_inf_nan=False, strict=True
    )


class ReccobeatsError(Exception):
    """A sanitized failure with an access-failure flag, without URLs or response bodies."""

    def __init__(self, message: str, *, credential: bool = False) -> None:
        """Retain whether callers should stop further access attempts."""
        super().__init__(message)
        self.credential = credential


class ReccobeatsService:
    """Reuse an injected HTTP client with serialized pacing shared by both endpoints."""

    def __init__(self, http_client: httpx.AsyncClient, *, request_interval: float = 1.0) -> None:
        """Configure pacing without taking ownership of the client's lifecycle."""
        if not math.isfinite(request_interval) or request_interval < 0:
            raise ValueError("request_interval must be finite and nonnegative")
        self.http_client = http_client
        self.request_interval = request_interval
        self.next_request_at = 0.0
        self._request_lock = anyio.Lock()

    async def get_audio_features(self, ids: Sequence[str]) -> dict[str, Any]:
        """Return the provider envelope for up to forty track IDs; 404 means no coverage."""
        if isinstance(ids, (str, bytes)) or len(ids) > 40:
            raise ValueError("audio-feature requests require at most 40 IDs")
        if any(not isinstance(value, str) or not value or "," in value for value in ids):
            raise ValueError("audio-feature IDs must be nonempty strings without commas")
        if not ids:
            return {"content": []}
        return await self._request("/audio-features", {"ids": ",".join(ids)}, empty_on_404=True)

    async def get_recommendation(self, request: RecommendationRequest) -> dict[str, Any]:
        """Return recommended track metadata, always excluding negative seeds from the interface."""
        request = RecommendationRequest.model_validate(request.model_dump())
        params: dict[str, str | list[str]] = {
            key: str(value)
            for key, value in request.model_dump(by_alias=True, exclude_none=True).items()
            if key != "seeds"
        }
        # httpx omits empty lists, avoiding an empty-string seed rejected by the provider.
        # We don't want to hide this option from the agent for now.
        params.update(seeds=",".join(request.seeds), negativeSeeds=[])
        return await self._request("/track/recommendation", params)

    async def _request(
        self, path: str, params: dict[str, str | list[str]], *, empty_on_404: bool = False
    ) -> dict[str, Any]:
        """Retry transient failures while retaining cooldown even after retry exhaustion."""
        async with self._request_lock:
            for attempt in range(3):
                await anyio.sleep(max(0.0, self.next_request_at - time.monotonic()))
                self.next_request_at = time.monotonic() + self.request_interval
                retry_delay = float(2**attempt)
                try:
                    response = await self.http_client.get(API_BASE + path, params=params)
                except httpx.TransportError:
                    failure = ReccobeatsError("reccobeats transport error")
                else:
                    status = response.status_code
                    if status in (401, 403):
                        raise ReccobeatsError("reccobeats credentials rejected", credential=True)
                    if status == 404 and empty_on_404:
                        return {"content": []}
                    if status != 429 and not 500 <= status <= 599:
                        if not response.is_success:
                            raise ReccobeatsError(f"reccobeats HTTP {status}")
                        try:
                            payload = response.json()
                        except ValueError:
                            raise ReccobeatsError("reccobeats returned invalid JSON") from None
                        if not isinstance(payload, dict):
                            raise ReccobeatsError("reccobeats returned an invalid response")
                        if not isinstance(payload.get("content"), list):
                            raise ReccobeatsError("reccobeats returned malformed content")
                        return payload
                    failure = ReccobeatsError(f"reccobeats HTTP {status}")
                    retry_delay = self._retry_delay(response, retry_delay)
                self.next_request_at = max(self.next_request_at, time.monotonic() + retry_delay)
                if attempt == 2:
                    raise failure from None
        raise AssertionError("unreachable retry state")

    @staticmethod
    def _retry_delay(response: httpx.Response, fallback: float) -> float:
        """Interpret numeric and HTTP-date Retry-After headers with a finite fallback."""
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return fallback
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return max(0.0, delay) if math.isfinite(delay) else fallback
