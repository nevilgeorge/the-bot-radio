"""Shared types for input services: settings, the run context, and the `Reading` result."""

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from the_bot_radio.web import config as web_config

TZ = web_config.TZ
USER_AGENT = "TheBotRadio/0.1 (thebotradio.com, liven93@gmail.com)"
HTTP_TIMEOUT = 10.0


def _env_float(env_var_name: str, default_value: float) -> float:
    """Read a float from the environment, falling back to `default_value` when unset."""
    return float(os.environ.get(env_var_name, default_value))


@dataclass(frozen=True)
class Settings:
    """Per-deployment location and credentials, read from environment variables."""

    lat: float = field(default_factory=lambda: _env_float("GREENPOINT_LAT", 40.7295))
    lon: float = field(default_factory=lambda: _env_float("GREENPOINT_LON", -73.9540))
    noaa_tide_station: str | None = field(
        default_factory=lambda: os.environ.get("NOAA_TIDE_STATION")
    )
    # The neighborhood's ZIP: 311 requests and Citi Bike docks (needs geo/<zip>.json).
    zip_code: str = field(default_factory=lambda: os.environ.get("BOT_RADIO_ZIP", "11222"))
    nyc_open_data_app_token: str | None = field(
        default_factory=lambda: os.environ.get("NYC_OPEN_DATA_APP_TOKEN")
    )


class Reading(BaseModel):
    """One input as read at run time. The generation step adds effect + weight."""

    key: str
    label: str
    value: str  # short human display string: "52°F, overcast"
    status: Literal["ok", "skipped", "stale"] = "ok"
    data: dict[str, Any] = Field(default_factory=dict)  # normalized fields for the LLM
    raw: dict[str, Any] = Field(default_factory=dict)  # trimmed source payload
    error: str | None = None
    fetched_at: datetime


@dataclass
class RunContext:
    """Everything an input service needs for one run: the date, clock, HTTP client, settings."""

    run_date: date  # America/New_York date of the run
    now: datetime  # timezone-aware
    http: httpx.AsyncClient
    settings: Settings = field(default_factory=Settings)
    data_dir: Path = web_config.DATA_DIR

    def reading(self, service: "InputService", value: str, **reading_fields: Any) -> Reading:
        """Build a `Reading` stamped with the service's key/label and this run's time."""
        return Reading(
            key=service.key,
            label=service.label,
            value=value,
            fetched_at=self.now,
            **reading_fields,
        )


class InputService(Protocol):
    """The interface every input service implements."""

    key: ClassVar[str]
    label: ClassVar[str]
    # Optional `timeout: ClassVar[float]` overrides runner.SERVICE_TIMEOUT for slow sources.

    async def fetch(self, ctx: RunContext) -> Reading:
        """Read this input for the run; raise on failure so the runner marks it skipped."""
        ...


def make_http_client(**client_kwargs: Any) -> httpx.AsyncClient:
    """Create the shared HTTP client with our timeout and User-Agent (NWS requires one)."""
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        **client_kwargs,
    )
