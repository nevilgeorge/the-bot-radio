"""Offline generation backend selection and SDK lifecycle checks."""

import asyncio
import threading
from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from the_bot_radio.generation import config, daily
from the_bot_radio.generation.storage import LocalDailyRecordStore, LocalRunArtifactStore

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("bucket", [None, ""])
async def test_local_mode_never_initializes_aws(tmp_path, monkeypatch, bucket):
    environment = {} if bucket is None else {"BOT_RADIO_S3_BUCKET": bucket}
    environment["BOT_RADIO_RUN_DIR"] = "/ignored-in-local-mode"
    factory = Mock(side_effect=AssertionError("AWS initialized in local mode"))
    monkeypatch.setattr(config, "create_s3_client", factory)
    selection = config.GenerationStorageConfig.from_environment(tmp_path, environment)
    async with config.generation_stores(selection) as stores:
        assert isinstance(stores.daily_store, LocalDailyRecordStore)
        assert isinstance(stores.artifact_store, LocalRunArtifactStore)
        assert stores.staging_root == tmp_path / ".run-staging"
    factory.assert_not_called()


@pytest.mark.parametrize(
    "environment",
    [
        {"BOT_RADIO_S3_PREFIX": "isolated"},
        {"BOT_RADIO_S3_PREFIX": "/"},
        {"BOT_RADIO_S3_BUCKET": "s3://bucket"},
        {"BOT_RADIO_S3_BUCKET": "arn:aws:s3:::bucket"},
        {"BOT_RADIO_S3_BUCKET": "bucket/path"},
        {"BOT_RADIO_S3_BUCKET": " bucket "},
        {"BOT_RADIO_S3_BUCKET": "bucket", "BOT_RADIO_S3_PREFIX": "a//b"},
    ],
)
def test_invalid_configuration_fails_before_aws(tmp_path, environment):
    with pytest.raises(ValueError):
        config.GenerationStorageConfig.from_environment(tmp_path, environment)


@pytest.mark.parametrize("failure", [False, True])
async def test_one_client_owned_until_generation_finishes(tmp_path, monkeypatch, failure):
    loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []
    client = Mock()
    client.close.side_effect = lambda: calls.append(("close", threading.get_ident()))

    def create_client():
        """Capture factory thread without discovering credentials."""
        calls.append(("create", threading.get_ident()))
        return client

    monkeypatch.setattr(config, "create_s3_client", create_client)
    selection = config.GenerationStorageConfig.from_environment(
        tmp_path,
        {
            "BOT_RADIO_S3_BUCKET": "example-bucket",
            "BOT_RADIO_S3_PREFIX": "/isolated/",
            "BOT_RADIO_RUN_DIR": str(tmp_path / "custom"),
        },
    )
    assert selection.prefix == "isolated"
    try:
        async with config.generation_stores(selection) as stores:
            assert stores.daily_store.client is client
            assert stores.artifact_store.client is client
            assert stores.staging_root == tmp_path / "custom"
            client.close.assert_not_called()
            if failure:
                raise RuntimeError("generation failed")
    except RuntimeError:
        assert failure
    assert [name for name, _ in calls] == ["create", "close"]
    assert all(thread_id != loop_thread for _, thread_id in calls)


async def test_cancelled_client_creation_drains_then_closes(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    client = Mock()

    def create_client():
        """Hold startup until cancellation has reached its owner."""
        started.set()
        assert release.wait(5)
        return client

    monkeypatch.setattr(config, "create_s3_client", create_client)
    selection = config.GenerationStorageConfig.from_environment(
        tmp_path, {"BOT_RADIO_S3_BUCKET": "example-bucket"}
    )

    async def own_client():
        """Attempt acquisition under a cancellable CLI task."""
        async with config.generation_stores(selection):
            pytest.fail("cancelled startup yielded stores")

    task = asyncio.create_task(own_client())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.close.assert_called_once()


def test_sdk_configuration_uses_normal_resolution(monkeypatch):
    import boto3

    session = Mock()
    session_factory = Mock(return_value=session)
    monkeypatch.setattr(boto3.session, "Session", session_factory)
    assert config.create_s3_client() is session.client.return_value
    session_factory.assert_called_once_with()
    arguments, options = session.client.call_args
    assert arguments == ("s3",)
    assert set(options) == {"config"}
    sdk_config = options["config"]
    assert sdk_config.retries == {"mode": "standard", "total_max_attempts": 1}
    assert sdk_config.connect_timeout == 1
    assert sdk_config.read_timeout == 2


def test_cli_passes_s3_stores_and_prints_uri(tmp_path, monkeypatch, capsys):
    client = Mock()
    monkeypatch.setattr(config, "create_s3_client", Mock(return_value=client))
    monkeypatch.setenv("BOT_RADIO_S3_BUCKET", "example-bucket")
    monkeypatch.setenv("BOT_RADIO_S3_PREFIX", "trial")
    monkeypatch.setenv("BOT_RADIO_RUN_DIR", str(tmp_path / "retained"))
    generation = AsyncMock(return_value="s3://example-bucket/trial/days/2026-10-27.json")
    monkeypatch.setattr(daily, "generate_day", generation)
    assert daily.main(["--date", "2026-10-27", "--data-dir", str(tmp_path)]) == 0
    options = generation.call_args.kwargs
    assert generation.call_args.args == (date(2026, 10, 27),)
    assert options["daily_store"].client is client
    assert options["artifact_store"].client is client
    assert options["staging_root"] == tmp_path / "retained"
    assert options["resources_dir"] == daily.RESOURCE_DIR
    client.close.assert_called_once()
    assert capsys.readouterr().out == "s3://example-bucket/trial/days/2026-10-27.json\n"


async def test_cleanup_preserves_generation_failure(tmp_path, monkeypatch):
    client = Mock()
    client.close.side_effect = OSError("close failed")
    monkeypatch.setattr(config, "create_s3_client", Mock(return_value=client))
    selection = config.GenerationStorageConfig.from_environment(
        tmp_path, {"BOT_RADIO_S3_BUCKET": "example-bucket"}
    )
    failure = RuntimeError("generation failed")
    with pytest.raises(RuntimeError) as caught:
        async with config.generation_stores(selection):
            raise failure
    assert caught.value is failure
    assert caught.value.__notes__ == ["S3 client cleanup failed: OSError"]


async def test_cancelled_closure_drains_client_worker(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    def close_client():
        """Keep cleanup running until the cancellation has been observed."""
        started.set()
        assert release.wait(5)
        closed.set()

    client = Mock(close=Mock(side_effect=close_client))
    monkeypatch.setattr(config, "create_s3_client", Mock(return_value=client))
    selection = config.GenerationStorageConfig.from_environment(
        tmp_path, {"BOT_RADIO_S3_BUCKET": "example-bucket"}
    )

    async def own_client():
        """Acquire and immediately finalize the selected stores."""
        async with config.generation_stores(selection):
            pass

    task = asyncio.create_task(own_client())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
