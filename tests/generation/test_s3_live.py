"""Opt-in S3 qualification with fake providers and permanently retained UUID prefixes.

Run only these tests with ``RUN_LIVE=1 uv run pytest -m s3_live -s``. Set
BOT_RADIO_S3_BUCKET explicitly; normal SDK credentials and region resolution apply.
No test deletes objects or accesses preexisting canonical records.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_daily import NOW, RUN_DATE, staged_diagnostics
from test_daily import generation_services as generation_services
from test_preparation import published_record

from the_bot_radio.generation.config import GenerationStorageConfig, create_s3_client
from the_bot_radio.generation.daily import generate_day
from the_bot_radio.generation.models import DailyRecord
from the_bot_radio.generation.runtime import RunRecorder, recover_run_artifacts
from the_bot_radio.generation.s3_storage import S3DailyRecordStore, S3RunArtifactStore
from the_bot_radio.generation.storage import RunArtifactStore, run_in_thread
from the_bot_radio.web.main import create_app

pytestmark = [pytest.mark.anyio, pytest.mark.s3_live]


@dataclass
class QualificationStorage:
    """Own the real SDK client and an isolated, retained qualification namespace."""

    client: Any
    bucket: str
    prefix: str

    @property
    def daily(self) -> S3DailyRecordStore:
        """Return the real daily adapter scoped to this execution only."""
        return S3DailyRecordStore(self.client, self.bucket, self.prefix)

    @property
    def artifacts(self) -> S3RunArtifactStore:
        """Return the real artifact adapter scoped to this execution only."""
        return S3RunArtifactStore(self.client, self.bucket, self.prefix)

    async def put_day(self, playlist_date: date, content: bytes) -> None:
        """Set up a fixture under the unique namespace, preserving all versions."""
        key = f"{self.prefix}/days/{playlist_date.isoformat()}.json"
        assert key.startswith(self.prefix + "/")
        await run_in_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType="application/json",
        )

    async def read_artifact(self, relative_key: str) -> bytes:
        """Read and close one retained artifact in a worker."""
        assert relative_key.startswith("runs/")

        def read_body() -> bytes:
            """Consume the stream while its client remains owned by the fixture."""
            response = self.client.get_object(
                Bucket=self.bucket, Key=f"{self.prefix}/{relative_key}"
            )
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()

        return await run_in_thread(read_body)


@pytest.fixture
async def qualification_storage(tmp_path) -> AsyncIterator[QualificationStorage]:
    """Require explicit configuration and print retained locations even on failure."""
    if os.environ.get("RUN_LIVE") != "1":
        pytest.skip("S3 qualification requires RUN_LIVE=1")
    configuration = GenerationStorageConfig.from_environment(tmp_path, os.environ)
    if configuration.bucket is None:
        pytest.fail("S3 qualification blocked: BOT_RADIO_S3_BUCKET must be explicitly configured")
    prefix = "/".join(filter(None, (configuration.prefix, "qualification", uuid.uuid4().hex)))
    location = f"s3://{configuration.bucket}/{prefix}/"
    print(f"S3 qualification prefix: {prefix}; retained staging location: {location}")
    clients: list[Any] = []

    def capture_client() -> None:
        """Retain ownership if cancellation arrives during SDK construction."""
        clients.append(create_s3_client())

    try:
        await run_in_thread(capture_client)
        print(f"S3 qualification region: {clients[0].meta.region_name}")
        yield QualificationStorage(clients[0], configuration.bucket, prefix)
    finally:
        try:
            if clients:
                await run_in_thread(clients[0].close)
        finally:
            print(f"Retained S3 qualification data (no deletion): {location}")


async def generate_in_staging(
    services: SimpleNamespace,
    storage: QualificationStorage,
    *,
    artifact_store: RunArtifactStore | None = None,
) -> str:
    """Run real orchestration and S3 persistence with deterministic fake providers."""
    return await generate_day(
        services.data_dir,
        RUN_DATE,
        now=NOW,
        claude_service=services.claude,
        spotify_service=services.spotify,
        playlist_agent=services.agent,
        daily_store=storage.daily,
        artifact_store=artifact_store or storage.artifacts,
    )


async def test_s3_live_generation_history_and_audited_skip(
    generation_services: SimpleNamespace, qualification_storage: QualificationStorage
) -> None:
    """Qualify canonical bytes, original history evidence, and a provider-free rerun."""
    services = generation_services
    storage = qualification_storage
    published_date = RUN_DATE - timedelta(days=2)
    pending_date = RUN_DATE - timedelta(days=1)
    history_content = (
        " \n" + json.dumps(published_record(published_date), indent=3) + " \n"
    ).encode()
    pending_payload = published_record(pending_date)
    pending_payload.update(publication_status="pending", spotify_playlist_id=None)
    await storage.put_day(published_date, history_content)
    await storage.put_day(pending_date, json.dumps(pending_payload).encode())

    location = await generate_in_staging(services, storage)
    assert location == storage.daily.storage_location(RUN_DATE)
    committed_content = await storage.daily.read(RUN_DATE)
    assert committed_content is not None
    record = DailyRecord.model_validate_json(committed_content)
    assert committed_content == (record.model_dump_json(indent=2) + "\n").encode()
    assert record.publication_status == "pending"
    prepared_run = services.prepared_runs[-1]
    history_directory = prepared_run.workspace / "history"
    assert (history_directory / f"{published_date}.json").read_bytes() == history_content
    assert not (history_directory / f"{pending_date}.json").exists()
    context = json.loads((prepared_run.workspace / "context.json").read_bytes())
    assert context["history"]["included_dates"] == [published_date.isoformat()]
    assert context["history"]["excluded_dates"] == [pending_date.isoformat()]
    accepted_directory, accepted_outcome = staged_diagnostics(services)
    assert accepted_outcome["status"] == "accepted"
    retained_history = await storage.read_artifact(
        f"runs/{RUN_DATE}/{accepted_outcome['run_id']}/workspace/history/{published_date}.json"
    )
    assert retained_history == history_content

    provider_calls = (
        services.agent.run.await_count,
        services.claude.score_mood.await_count,
        services.spotify.search_tracks.await_count,
    )
    assert await generate_in_staging(services, storage) == location
    assert await storage.daily.read(RUN_DATE) == committed_content
    assert (
        provider_calls
        == (
            services.agent.run.await_count,
            services.claude.score_mood.await_count,
            services.spotify.search_tracks.await_count,
        )
        == (1, 1, 1)
    )
    diagnostics_paths = sorted(accepted_directory.parent.glob("*/diagnostics.json"))
    outcomes = [json.loads(path.read_bytes()) for path in diagnostics_paths]
    assert {outcome["status"] for outcome in outcomes} == {"accepted", "skipped"}
    assert len({outcome["run_id"] for outcome in outcomes}) == 2
    for path, outcome in zip(diagnostics_paths, outcomes, strict=True):
        remote_content = await storage.read_artifact(
            f"runs/{RUN_DATE}/{outcome['run_id']}/diagnostics.json"
        )
        assert remote_content == path.read_bytes()
    skipped = next(outcome for outcome in outcomes if outcome["status"] == "skipped")
    assert skipped["reason"] == "existing_daily_record"
    assert not (services.data_dir / "days").exists()

    application = create_app(
        GenerationStorageConfig(
            services.data_dir, storage.bucket, storage.prefix, services.data_dir / ".run-staging"
        )
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://qualification.local"
        ) as web_client:
            assert (await web_client.get("/healthz")).json() == {
                "ok": True,
                "latest_day": published_date.isoformat(),
            }
            assert (await web_client.get(f"/index/{RUN_DATE}")).status_code == 404
            assert (await web_client.get(f"/index/{pending_date}")).status_code == 404
            # Fixture replacement only: this does not implement a publisher.
            published_payload = json.loads(committed_content)
            published_payload.update(publication_status="published", spotify_playlist_id="p" * 22)
            await storage.put_day(RUN_DATE, json.dumps(published_payload).encode())
            await application.state.daily_cache.refresh()
            assert (await web_client.get("/healthz")).json() == {
                "ok": True,
                "latest_day": RUN_DATE.isoformat(),
            }
            day_response = await web_client.get(f"/index/{RUN_DATE}")
            assert day_response.status_code == 200
            assert record.title in day_response.text
            assert (await web_client.get("/")).status_code == 200


class FailingArtifactStore:
    """Inject a repeatable artifact failure while all successful writes use real S3."""

    def __init__(self, artifact_store: RunArtifactStore, fail_at: str) -> None:
        """Retain the production adapter and one orchestration failure boundary."""
        self.artifact_store = artifact_store
        self.fail_at = fail_at

    async def write(self, run_date: date, run_id: str, relative_name: str, content: bytes) -> str:
        """Reject proposal or accepted diagnostics uploads without mutating canonical data."""
        if (
            self.fail_at == "precommit"
            and relative_name == "proposal.json"
            or self.fail_at == "postcommit"
            and relative_name == "diagnostics.json"
            and json.loads(content).get("status") == "accepted"
        ):
            raise OSError(f"injected {self.fail_at} artifact failure")
        return await self.artifact_store.write(run_date, run_id, relative_name, content)


@pytest.mark.parametrize("fail_at", ["precommit", "postcommit"])
async def test_s3_live_artifact_recovery_preserves_original_identity(
    generation_services: SimpleNamespace,
    qualification_storage: QualificationStorage,
    monkeypatch: pytest.MonkeyPatch,
    fail_at: str,
) -> None:
    """Recover retained artifacts without another provider call or canonical mutation."""
    services = generation_services
    storage = qualification_storage
    monkeypatch.setattr(RunRecorder, "retry_delay", 0)
    with pytest.raises(ExceptionGroup):
        await generate_in_staging(
            services,
            storage,
            artifact_store=FailingArtifactStore(storage.artifacts, fail_at),
        )
    staging_directory, outcome = staged_diagnostics(services)
    committed_content = await storage.daily.read(RUN_DATE)
    assert (committed_content is not None) is (fail_at == "postcommit")
    assert outcome["status"] == ("accepted" if fail_at == "postcommit" else "failed")
    original_run_id = outcome["run_id"]
    provider_calls = (
        services.agent.run.await_count,
        services.claude.score_mood.await_count,
        services.spotify.search_tracks.await_count,
    )
    await recover_run_artifacts(storage.artifacts, RUN_DATE, original_run_id, staging_directory)
    assert await storage.daily.read(RUN_DATE) == committed_content
    for relative_name in ("diagnostics.json", "transcript.jsonl", "proposal.json"):
        assert (
            await storage.read_artifact(f"runs/{RUN_DATE}/{original_run_id}/{relative_name}")
            == (staging_directory / relative_name).read_bytes()
        )
    assert (
        provider_calls
        == (
            services.agent.run.await_count,
            services.claude.score_mood.await_count,
            services.spotify.search_tracks.await_count,
        )
        == (1, 1, 1)
    )
    assert len(list(staging_directory.parent.iterdir())) == 1
