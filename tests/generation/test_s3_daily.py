"""Offline generation acceptance through the real S3 storage adapters."""

import asyncio
import io
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from test_daily import NOW, RUN_DATE, staged_diagnostics
from test_daily import generation_services as generation_services

from the_bot_radio.generation.daily import generate_day
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.runtime import RunRecorder, recover_run_artifacts
from the_bot_radio.generation.s3_storage import S3DailyRecordStore, S3RunArtifactStore

pytestmark = pytest.mark.anyio
BUCKET = "offline-generation-data"
PREFIX = "qualification"
DAILY_KEY = f"{PREFIX}/days/{RUN_DATE}.json"


def service_error(code: str, status: int, operation: str) -> ClientError:
    """Construct an SDK service error without credentials or network access."""
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation
    )


class FakeS3Client:
    """Store exact object bytes and script failures at orchestration boundaries."""

    def __init__(self, create_mode: str = "success", fail_at: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.read_keys: list[str] = []
        self.create_calls = 0
        self.create_mode = create_mode
        self.fail_at = fail_at
        self.creation_error = OSError("canonical acknowledgement lost")
        self.create_started = threading.Event()
        self.release_create = threading.Event()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return an independent body or the precise missing-key SDK error."""
        assert Bucket == BUCKET
        self.read_keys.append(Key)
        if Key not in self.objects:
            raise service_error("NoSuchKey", 404, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, **parameters: Any) -> dict[str, Any]:
        """Implement conditional creation separately from artifact replacements."""
        assert parameters["Bucket"] == BUCKET
        key = parameters["Key"]
        content = parameters["Body"]
        assert isinstance(content, bytes)
        if "IfNoneMatch" in parameters:
            assert parameters["IfNoneMatch"] == "*"
            self.create_calls += 1
            if self.create_mode.startswith("cancel"):
                self.create_started.set()
                if not self.release_create.wait(timeout=10):
                    raise TimeoutError("test did not release canonical creation")
            if self.create_mode in {
                "success",
                "conflict_valid",
                "ambiguous_commit",
                "cancel_commit",
                "409_commit",
            }:
                self.objects[key] = content
            elif self.create_mode == "conflict_invalid":
                self.objects[key] = b"malformed"
            if self.create_mode.startswith("conflict"):
                raise service_error("PreconditionFailed", 412, "PutObject")
            if self.create_mode.startswith("ambiguous"):
                raise self.creation_error
            if self.create_mode.startswith("409"):
                raise service_error("ConditionalRequestConflict", 409, "PutObject")
        else:
            should_fail = (
                self.fail_at == "precommit"
                and key.endswith("/proposal.json")
                or self.fail_at == "postcommit"
                and key.endswith("/diagnostics.json")
                and json.loads(content).get("status") == "accepted"
            )
            if should_fail:
                raise service_error("AccessDenied", 403, "PutObject")
            self.objects[key] = content
        return {}

    def close(self) -> None:
        """Satisfy the SDK client boundary without managing external resources."""


async def generate_in_s3(services: SimpleNamespace, client: FakeS3Client) -> str:
    """Run production orchestration with S3 adapters and fake provider services."""
    return await generate_day(
        RUN_DATE,
        local_work_root=services.data_dir / ".run-staging",
        resources_dir=services.data_dir,
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
        playlist_agent=services.agent,
        daily_store=S3DailyRecordStore(client, BUCKET, PREFIX),
        artifact_store=S3RunArtifactStore(client, BUCKET, PREFIX),
    )


async def test_s3_generation_and_audited_rerun_skip(generation_services: SimpleNamespace) -> None:
    client = FakeS3Client()
    location = await generate_in_s3(generation_services, client)
    assert location == f"s3://{BUCKET}/{DAILY_KEY}"
    record = DailyRecord.model_validate_json(client.objects[DAILY_KEY])
    assert record.date == RUN_DATE
    assert record.publication_status == "pending"
    assert not (generation_services.data_dir / "days").exists()
    assert not (generation_services.data_dir / "runs").exists()
    staging_directory, staged_outcome = staged_diagnostics(generation_services)
    assert staging_directory.is_relative_to(generation_services.data_dir / ".run-staging")
    assert (staging_directory / "workspace/catalog.jsonl").is_file()
    assert staged_outcome["status"] == "accepted"
    assert len(client.read_keys) == 31
    original_content = client.objects[DAILY_KEY]
    assert await generate_in_s3(generation_services, client) == location
    assert client.objects[DAILY_KEY] == original_content
    assert client.create_calls == 1
    generation_services.agent.run.assert_awaited_once()
    outcomes = [
        json.loads(content)
        for key, content in client.objects.items()
        if key.endswith("/diagnostics.json")
    ]
    assert {outcome["status"] for outcome in outcomes} == {"accepted", "skipped"}
    assert len({outcome["run_id"] for outcome in outcomes}) == 2


@pytest.mark.parametrize("create_mode", ["conflict_valid", "conflict_invalid", "conflict_missing"])
async def test_s3_conditional_conflict_rereads_and_validates(
    generation_services: SimpleNamespace, create_mode: str
) -> None:
    client = FakeS3Client(create_mode)
    if create_mode == "conflict_valid":
        assert await generate_in_s3(generation_services, client) == f"s3://{BUCKET}/{DAILY_KEY}"
        outcome = staged_diagnostics(generation_services)[1]
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "daily_record_created_elsewhere"
    else:
        with pytest.raises(ValueError):
            await generate_in_s3(generation_services, client)
        assert staged_diagnostics(generation_services)[1]["status"] == "failed"
    assert client.create_calls == 1
    assert client.read_keys.count(DAILY_KEY) == 2


@pytest.mark.parametrize(
    "create_mode", ["ambiguous_commit", "ambiguous_lost", "409_commit", "409_lost"]
)
async def test_s3_ambiguous_commit_reconciles_without_recreation(
    generation_services: SimpleNamespace, create_mode: str
) -> None:
    client = FakeS3Client(create_mode)
    committed = create_mode.endswith("commit")
    if committed:
        assert await generate_in_s3(generation_services, client) == f"s3://{BUCKET}/{DAILY_KEY}"
    else:
        with pytest.raises((OSError, ClientError)):
            await generate_in_s3(generation_services, client)
    outcome = staged_diagnostics(generation_services)[1]
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert outcome["reconciliation"] == ("exact_match" if committed else "absent")
    assert client.create_calls == 1
    assert client.read_keys.count(DAILY_KEY) == 2


@pytest.mark.parametrize("committed", [True, False])
async def test_s3_cancellation_drains_creation_before_reconciliation(
    generation_services: SimpleNamespace, committed: bool
) -> None:
    client = FakeS3Client("cancel_commit" if committed else "cancel_lost")
    generation_task = asyncio.create_task(generate_in_s3(generation_services, client))
    try:
        async with asyncio.timeout(5):
            while not client.create_started.is_set():
                await asyncio.sleep(0.001)
        generation_task.cancel()
        await asyncio.sleep(0)
        assert not generation_task.done()
    finally:
        client.release_create.set()
    with pytest.raises(asyncio.CancelledError):
        await generation_task
    outcome = staged_diagnostics(generation_services)[1]
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert outcome["committed"] is committed
    assert outcome["reconciliation"] == ("exact_match" if committed else "absent")
    assert client.create_calls == 1
    assert client.read_keys.count(DAILY_KEY) == 2


@pytest.mark.parametrize("fail_at", ["precommit", "postcommit"])
async def test_s3_artifact_failure_recovers_original_run_without_generation(
    generation_services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    client = FakeS3Client(fail_at=fail_at)
    with pytest.raises(ExceptionGroup) as caught:
        await generate_in_s3(generation_services, client)
    staging_directory, outcome = staged_diagnostics(generation_services)
    committed = fail_at == "postcommit"
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert (DAILY_KEY in client.objects) is committed
    assert client.create_calls == int(committed)
    assert any(str(staging_directory) in note for note in caught.value.__notes__)
    provider_calls = generation_services.agent.run.await_count
    client.fail_at = None
    original_run_id = outcome["run_id"]
    await recover_run_artifacts(
        S3RunArtifactStore(client, BUCKET, PREFIX),
        RUN_DATE,
        original_run_id,
        staging_directory,
    )
    artifact_prefix = f"{PREFIX}/runs/{RUN_DATE}/{original_run_id}/"
    artifacts = {key: content for key, content in client.objects.items() if "/runs/" in key}
    assert artifacts
    assert all(key.startswith(artifact_prefix) for key in artifacts)
    assert (
        json.loads(artifacts[artifact_prefix + "diagnostics.json"])["status"] == outcome["status"]
    )
    assert staging_directory.exists()
    assert generation_services.agent.run.await_count == provider_calls
    assert client.create_calls == int(committed)
