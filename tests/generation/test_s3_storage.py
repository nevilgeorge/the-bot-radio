"""Offline S3 request contracts, error handling, and worker cancellation behavior."""

import asyncio
from collections.abc import Iterator
from datetime import date
from io import BytesIO
from threading import Event, get_ident
from typing import Any
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from the_bot_radio.generation.s3_storage import S3DailyRecordStore, S3RunArtifactStore
from the_bot_radio.generation.storage import RecordExists

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)
BUCKET = "offline-radio-data"
DAY_KEY = "days/2026-10-27.json"


@pytest.fixture
def sdk_client() -> Iterator[Any]:
    """Supply explicit credentials and region without consulting any AWS providers."""
    client = boto3.client(
        "s3",
        aws_access_key_id="offline-access",
        aws_secret_access_key="offline-secret",
        region_name="us-east-1",
    )
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize("prefix", ["", "/", "staging", "/staging/", "/a/b/"])
def test_daily_location_is_pure_and_normalizes_prefix(prefix: str) -> None:
    """Computing canonical URIs requires neither SDK requests nor credentials."""
    sdk_client = Mock()
    normalized_prefix = prefix.strip("/")
    key = f"{normalized_prefix}/{DAY_KEY}" if normalized_prefix else DAY_KEY
    assert S3DailyRecordStore(sdk_client, BUCKET, prefix).storage_location(RUN_DATE) == (
        f"s3://{BUCKET}/{key}"
    )
    assert sdk_client.mock_calls == []


@pytest.mark.parametrize(
    "prefix",
    ["a//b", ".", "..", "a/../b", "a/./b", "a\\b", "a\nb", "a\x00b", "a\x7fb", "a\x85b"],
)
@pytest.mark.parametrize("store_class", [S3DailyRecordStore, S3RunArtifactStore])
def test_rejects_unsafe_prefixes(prefix: str, store_class: type) -> None:
    """A prefix cannot introduce ambiguous, traversing, or control components."""
    with pytest.raises(ValueError):
        store_class(Mock(), BUCKET, prefix)


async def test_read_returns_exact_bytes_and_closes_stream(sdk_client: Any) -> None:
    """GetObject bytes remain untouched and release their HTTP response body."""
    content = b'{ "unchanged": true }\n\x00'
    stream = BytesIO(content)
    body = StreamingBody(stream, len(content))
    with Stubber(sdk_client) as stubber:
        stubber.add_response("get_object", {"Body": body}, {"Bucket": BUCKET, "Key": DAY_KEY})
        assert await S3DailyRecordStore(sdk_client, BUCKET).read(RUN_DATE) == content
        stubber.assert_no_pending_responses()
    assert stream.closed


@pytest.mark.parametrize(
    "code,status",
    [
        ("NoSuchKey", 404),
        ("NoSuchBucket", 404),
        ("AccessDenied", 403),
        ("404", 404),
        ("InternalError", 500),
    ],
)
async def test_only_missing_key_is_optional(sdk_client: Any, code: str, status: int) -> None:
    """Missing buckets and denied reads must never masquerade as absent history."""
    with Stubber(sdk_client) as stubber:
        stubber.add_client_error(
            "get_object",
            service_error_code=code,
            http_status_code=status,
            expected_params={"Bucket": BUCKET, "Key": DAY_KEY},
        )
        daily_store = S3DailyRecordStore(sdk_client, BUCKET)
        if code == "NoSuchKey":
            assert await daily_store.read(RUN_DATE) is None
        else:
            with pytest.raises(ClientError) as caught:
                await daily_store.read(RUN_DATE)
            assert caught.value.response["Error"]["Code"] == code
        stubber.assert_no_pending_responses()


async def test_transport_read_failure_propagates() -> None:
    """A network failure is different from a missing object."""
    error = EndpointConnectionError(endpoint_url="https://offline.invalid")
    sdk_client = Mock(get_object=Mock(side_effect=error))
    with pytest.raises(EndpointConnectionError) as caught:
        await S3DailyRecordStore(sdk_client, BUCKET).read(RUN_DATE)
    assert caught.value is error


async def test_body_failure_closes_stream_inside_worker() -> None:
    """Body consumption and cleanup happen off the event loop even on failure."""
    event_loop_thread = get_ident()
    worker_threads: list[int] = []

    def fail_read() -> bytes:
        """Record the worker identity before failing to consume the body."""
        worker_threads.append(get_ident())
        raise OSError("body truncated")

    def close_body() -> None:
        """Record the cleanup thread as well as the read thread."""
        worker_threads.append(get_ident())

    body = Mock(read=Mock(side_effect=fail_read), close=Mock(side_effect=close_body))

    def get_object(**kwargs: Any) -> dict[str, Any]:
        """Record that the SDK request itself also runs in the worker."""
        worker_threads.append(get_ident())
        return {"Body": body}

    sdk_client = Mock(get_object=Mock(side_effect=get_object))
    with pytest.raises(OSError, match="body truncated"):
        await S3DailyRecordStore(sdk_client, BUCKET).read(RUN_DATE)
    body.close.assert_called_once()
    assert len(worker_threads) == 3
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)


async def test_create_uses_one_conditional_put(sdk_client: Any) -> None:
    """Canonical creation preserves exact bytes and delegates encryption to the bucket."""
    content = b'{"accepted":true}\n'
    with Stubber(sdk_client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "prefix/" + DAY_KEY,
                "Body": content,
                "ContentType": "application/json",
                "IfNoneMatch": "*",
            },
        )
        assert await S3DailyRecordStore(sdk_client, BUCKET, "/prefix/").create(
            RUN_DATE, content
        ) == (f"s3://{BUCKET}/prefix/{DAY_KEY}")
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "code,status",
    [
        ("PreconditionFailed", 412),
        ("412", 412),
        ("ConditionalRequestConflict", 409),
        ("AccessDenied", 403),
        ("InternalError", 500),
    ],
)
async def test_create_maps_only_precondition_failure(
    sdk_client: Any, code: str, status: int
) -> None:
    """A conflict or ambiguous service failure reaches the host without adapter retry."""
    with Stubber(sdk_client) as stubber:
        stubber.add_client_error(
            "put_object",
            service_error_code=code,
            http_status_code=status,
            expected_params={
                "Bucket": BUCKET,
                "Key": DAY_KEY,
                "Body": b"content",
                "ContentType": "application/json",
                "IfNoneMatch": "*",
            },
        )
        error_class = RecordExists if status == 412 else ClientError
        with pytest.raises(error_class):
            await S3DailyRecordStore(sdk_client, BUCKET).create(RUN_DATE, b"content")
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "name,content_type",
    [
        ("diagnostics.json", "application/json"),
        ("transcript.jsonl", "application/x-ndjson"),
        ("events.ndjson", "application/x-ndjson"),
        ("workspace/blob.bin", "application/octet-stream"),
    ],
)
async def test_artifact_put_replaces_snapshot(
    sdk_client: Any, name: str, content_type: str
) -> None:
    """Every snapshot uses an unconditional complete PutObject under its run identity."""
    key = f"prefix/runs/{RUN_DATE}/run_1-2/{name}"
    artifact_store = S3RunArtifactStore(sdk_client, BUCKET, "prefix")
    with Stubber(sdk_client) as stubber:
        for content in (b"old\x00", b"new\n"):
            stubber.add_response(
                "put_object",
                {},
                {"Bucket": BUCKET, "Key": key, "Body": content, "ContentType": content_type},
            )
            assert await artifact_store.write(RUN_DATE, "run_1-2", name, content) == (
                f"s3://{BUCKET}/{key}"
            )
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "run_id", ["", ".", "..", "../outside", "/absolute", "a/b", "a\\b", "a\nb"]
)
async def test_artifact_rejects_unsafe_run_id(run_id: str) -> None:
    """Run identifiers stay within one key component before any request is made."""
    sdk_client = Mock()
    with pytest.raises(ValueError, match="run ID"):
        await S3RunArtifactStore(sdk_client, BUCKET).write(RUN_DATE, run_id, "file", b"content")
    assert sdk_client.mock_calls == []


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "a/../b",
        "a/./b",
        "/absolute",
        "a//b",
        "a/",
        "a\\b",
        "a\nb",
        "a\x00b",
        "a\x7fb",
        "a\x85b",
    ],
)
async def test_artifact_rejects_unsafe_relative_names(name: str) -> None:
    """Artifact names cannot traverse, alias, or escape the selected run prefix."""
    sdk_client = Mock()
    with pytest.raises(ValueError, match="artifact name"):
        await S3RunArtifactStore(sdk_client, BUCKET).write(RUN_DATE, "run", name, b"content")
    assert sdk_client.mock_calls == []


@pytest.mark.parametrize("operation", ["create", "write"])
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_mutation_drains_worker_before_propagation(
    operation: str, worker_fails: bool
) -> None:
    """Cancellation joins an in-flight mutation before allowing a newer snapshot."""
    started = Event()
    release = Event()
    completed: list[bytes] = []
    event_loop_thread = get_ident()

    def put_object(**kwargs: Any) -> dict[str, Any]:
        """Block the old write while the event loop processes cancellation."""
        assert get_ident() != event_loop_thread
        content = kwargs["Body"]
        if content == b"old":
            started.set()
            assert release.wait(timeout=3)
            if worker_fails:
                raise OSError("cancelled worker failed")
        completed.append(content)
        return {}

    sdk_client = Mock(put_object=Mock(side_effect=put_object))
    daily_store = S3DailyRecordStore(sdk_client, BUCKET)
    artifact_store = S3RunArtifactStore(sdk_client, BUCKET)
    mutation = (
        daily_store.create(RUN_DATE, b"old")
        if operation == "create"
        else artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"old")
    )
    mutation_task = asyncio.create_task(mutation)
    try:
        assert await asyncio.to_thread(started.wait, 1)
        mutation_task.cancel()
        await asyncio.sleep(0)
        assert not mutation_task.done()
        mutation_task.cancel()
        await asyncio.sleep(0)
        assert not mutation_task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await mutation_task
    await artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"new")
    assert completed == ([b"new"] if worker_fails else [b"old", b"new"])


async def test_blocked_body_read_keeps_event_loop_responsive() -> None:
    """Reading and closing a streaming body cannot stall unrelated async work."""
    started = Event()
    release = Event()
    closed = Event()

    def read_body() -> bytes:
        """Hold the response body in a worker until the async test releases it."""
        started.set()
        assert release.wait(timeout=3)
        return b"complete"

    body = Mock(read=Mock(side_effect=read_body), close=Mock(side_effect=closed.set))
    sdk_client = Mock(get_object=Mock(return_value={"Body": body}))
    read_task = asyncio.create_task(S3DailyRecordStore(sdk_client, BUCKET).read(RUN_DATE))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        assert not read_task.done()
    finally:
        release.set()
    assert await read_task == b"complete"
    assert closed.is_set()
