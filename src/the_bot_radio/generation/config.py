"""Share optional storage configuration and SDK construction across application entry points."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from the_bot_radio.generation.s3_listing import ListingClient
from the_bot_radio.generation.s3_storage import (
    S3Client,
    S3DailyRecordStore,
    S3RunArtifactStore,
    normalize_s3_prefix,
    validate_s3_bucket,
)
from the_bot_radio.generation.storage import (
    DailyRecordStore,
    LocalDailyRecordStore,
    LocalRunArtifactStore,
    RunArtifactStore,
    run_in_thread,
)


@dataclass(frozen=True)
class GenerationStorageConfig:
    """Storage selection resolved once after application dotenv loading."""

    data_dir: Path
    bucket: str | None
    prefix: str
    local_work_root: Path

    @classmethod
    def from_environment(
        cls, data_dir: Path, environment: Mapping[str, str]
    ) -> GenerationStorageConfig:
        """Validate configuration without constructing an AWS session or client."""
        bucket = environment.get("BOT_RADIO_S3_BUCKET") or None
        raw_prefix = environment.get("BOT_RADIO_S3_PREFIX", "")
        if raw_prefix and bucket is None:
            raise ValueError("BOT_RADIO_S3_PREFIX requires BOT_RADIO_S3_BUCKET")
        if bucket is not None:
            validate_s3_bucket(bucket)
        prefix = normalize_s3_prefix(raw_prefix)
        local_work_override = environment.get("BOT_RADIO_RUN_DIR") if bucket else None
        local_work_root = (
            Path(local_work_override) if local_work_override else data_dir / ".run-staging"
        )
        return cls(data_dir, bucket, prefix, local_work_root)


@dataclass(frozen=True)
class GenerationStores:
    """Selected byte stores and retained local work root."""

    daily_store: DailyRecordStore
    artifact_store: RunArtifactStore
    local_work_root: Path


class S3SDKClient(S3Client, ListingClient, Protocol):
    """Compose SDK capabilities without changing either storage contract."""


def create_s3_client() -> S3SDKClient:
    """Construct a client in a worker using normal SDK region and credential resolution."""
    import boto3
    from botocore.config import Config

    return boto3.session.Session().client(
        "s3",
        config=Config(
            retries={"mode": "standard", "total_max_attempts": 1},
            connect_timeout=1,
            read_timeout=2,
        ),
    )


@asynccontextmanager
async def generation_stores(config: GenerationStorageConfig) -> AsyncIterator[GenerationStores]:
    """Keep the SDK client alive through recorder finalization, then close it off-loop."""
    if config.bucket is None:
        yield GenerationStores(
            LocalDailyRecordStore(config.data_dir),
            LocalRunArtifactStore(config.data_dir),
            config.local_work_root,
        )
        return

    clients: list[S3Client] = []
    primary_error: BaseException | None = None

    def capture_client() -> None:
        """Retain ownership even if cancellation arrives during client construction."""
        clients.append(create_s3_client())

    try:
        await run_in_thread(capture_client)
        client = clients[0]
        yield GenerationStores(
            S3DailyRecordStore(client, config.bucket, config.prefix),
            S3RunArtifactStore(client, config.bucket, config.prefix),
            config.local_work_root,
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if clients:
            try:
                await run_in_thread(clients[0].close)
            except BaseException as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"S3 client cleanup failed: {type(error).__name__}")
