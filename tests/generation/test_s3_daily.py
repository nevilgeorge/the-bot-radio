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

from the_bot_radio.generation.agent import PlaylistAgentResult
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

    def __init__(self, write_mode: str = "success", fail_at: str | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.read_keys: list[str] = []
        self.write_calls = 0
        self.write_mode = write_mode
        self.fail_at = fail_at
        self.write_error = OSError("canonical acknowledgement lost")
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Return an independent body or the precise missing-key SDK error."""
        assert Bucket == BUCKET
        self.read_keys.append(Key)
        if Key not in self.objects:
            raise service_error("NoSuchKey", 404, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, **parameters: Any) -> dict[str, Any]:
        """Replace daily and artifact objects with independently scripted failures."""
        assert parameters["Bucket"] == BUCKET
        key = parameters["Key"]
        content = parameters["Body"]
        assert isinstance(content, bytes)
        assert "IfNoneMatch" not in parameters
        if key == DAILY_KEY:
            self.write_calls += 1
            if self.write_mode.startswith("cancel"):
                self.write_started.set()
                if not self.release_write.wait(timeout=10):
                    raise TimeoutError("test did not release canonical replacement")
            if self.write_mode in {
                "success",
                "ambiguous_commit",
                "cancel_commit",
                "409_commit",
            }:
                self.objects[key] = content
            if self.write_mode.startswith("ambiguous"):
                raise self.write_error
            if self.write_mode.startswith("409"):
                raise service_error("ConditionalRequestConflict", 409, "PutObject")
        else:
            should_fail = (
                self.fail_at == "precommit"
                and key.endswith(f"/{RUN_DATE}.json")
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


async def test_s3_generation_replaces_day_and_archives_each_output(
    generation_services: SimpleNamespace,
) -> None:
    client = FakeS3Client()
    location = await generate_in_s3(generation_services, client)
    assert location == f"s3://{BUCKET}/{DAILY_KEY}"
    record = DailyRecord.model_validate_json(client.objects[DAILY_KEY])
    assert record.date == RUN_DATE
    assert record.publication_status == "published"
    assert not (generation_services.data_dir / "days").exists()
    assert not (generation_services.data_dir / "runs").exists()
    staging_directory, staged_outcome = staged_diagnostics(generation_services)
    assert staging_directory.is_relative_to(generation_services.data_dir / ".run-staging")
    assert (staging_directory / "workspace/catalog.jsonl").is_file()
    assert staged_outcome["status"] == "accepted"
    assert len(client.read_keys) == 30
    original_content = client.objects[DAILY_KEY]

    async def regenerate(**kwargs: Any) -> PlaylistAgentResult:
        """Produce a distinct validated playlist for the next invocation."""
        result = await generation_services.run_agent(**kwargs)
        proposal = result.proposal.model_copy(update={"title": "A fresh rainy playlist"})
        kwargs["proposal_sink"](proposal)
        return PlaylistAgentResult(proposal=proposal, prompt=result.prompt)

    generation_services.agent.run.side_effect = regenerate
    assert await generate_in_s3(generation_services, client) == location
    replacement_content = client.objects[DAILY_KEY]
    assert replacement_content != original_content
    assert DailyRecord.model_validate_json(replacement_content).title == (
        f"{RUN_DATE} — A fresh rainy playlist"
    )
    assert client.write_calls == 2
    assert generation_services.agent.run.await_count == 2
    archived_outputs = [
        content
        for key, content in client.objects.items()
        if "/runs/" in key and key.endswith(f"/{RUN_DATE}.json")
    ]
    assert len(archived_outputs) == 2
    assert set(archived_outputs) == {original_content, replacement_content}
    outcomes = [
        json.loads(content)
        for key, content in client.objects.items()
        if key.endswith("/diagnostics.json")
    ]
    assert {outcome["status"] for outcome in outcomes} == {"accepted"}
    assert len({outcome["run_id"] for outcome in outcomes}) == 2


async def test_s3_malformed_existing_record_is_replaced(
    generation_services: SimpleNamespace,
) -> None:
    """Current-day bytes are replaced without requiring them to parse first."""
    client = FakeS3Client()
    client.objects[DAILY_KEY] = b"malformed"
    assert await generate_in_s3(generation_services, client) == f"s3://{BUCKET}/{DAILY_KEY}"
    DailyRecord.model_validate_json(client.objects[DAILY_KEY])
    assert client.write_calls == 1
    assert DAILY_KEY not in client.read_keys


@pytest.mark.parametrize(
    "write_mode", ["ambiguous_commit", "ambiguous_lost", "409_commit", "409_lost"]
)
async def test_s3_ambiguous_commit_reconciles_without_replacement_retry(
    generation_services: SimpleNamespace, write_mode: str
) -> None:
    client = FakeS3Client(write_mode)
    committed = write_mode.endswith("commit")
    if committed:
        assert await generate_in_s3(generation_services, client) == f"s3://{BUCKET}/{DAILY_KEY}"
    else:
        with pytest.raises((OSError, ClientError)):
            await generate_in_s3(generation_services, client)
    outcome = staged_diagnostics(generation_services)[1]
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert outcome["reconciliation"] == ("exact_match" if committed else "absent")
    assert client.write_calls == 1
    assert client.read_keys.count(DAILY_KEY) == 1


@pytest.mark.parametrize("committed", [True, False])
async def test_s3_cancellation_drains_replacement_before_reconciliation(
    generation_services: SimpleNamespace, committed: bool
) -> None:
    client = FakeS3Client("cancel_commit" if committed else "cancel_lost")
    generation_task = asyncio.create_task(generate_in_s3(generation_services, client))
    try:
        async with asyncio.timeout(5):
            while not client.write_started.is_set():
                await asyncio.sleep(0.001)
        generation_task.cancel()
        await asyncio.sleep(0)
        assert not generation_task.done()
    finally:
        client.release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await generation_task
    outcome = staged_diagnostics(generation_services)[1]
    assert outcome["status"] == ("accepted" if committed else "failed")
    assert outcome["committed"] is committed
    assert outcome["reconciliation"] == ("exact_match" if committed else "absent")
    assert client.write_calls == 1
    assert client.read_keys.count(DAILY_KEY) == 1


@pytest.mark.parametrize("fail_at", ["precommit", "postcommit"])
async def test_s3_artifact_failure_recovers_original_run_without_generation(
    generation_services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    client = FakeS3Client(fail_at=fail_at)
    previous_content = b"previous daily record"
    client.objects[DAILY_KEY] = previous_content
    with pytest.raises(ExceptionGroup) as caught:
        await generate_in_s3(generation_services, client)
    staging_directory, outcome = staged_diagnostics(generation_services)
    committed = fail_at == "postcommit"
    assert outcome["status"] == ("accepted" if committed else "failed")
    daily_content = client.objects[DAILY_KEY]
    assert (daily_content != previous_content) is committed
    assert client.write_calls == int(committed)
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
    assert client.write_calls == int(committed)
    assert client.objects[DAILY_KEY] == daily_content
    final_name = f"{RUN_DATE}.json"
    assert artifacts[artifact_prefix + final_name] == (staging_directory / final_name).read_bytes()
    if committed:
        assert artifacts[artifact_prefix + final_name] == daily_content
