"""Daily snapshot discovery, invalidation, failure isolation, and worker lifetime."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from the_bot_radio.generation.s3_listing import DailyObjectDescriptor, S3DailyListing
from the_bot_radio.generation.s3_storage import S3DailyRecordStore
from the_bot_radio.web.daily_cache import S3DailyCache

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)
MODIFIED = datetime(2026, 10, 27, tzinfo=UTC)
BUCKET = "offline-radio-data"


def descriptor(run_date: date = RUN_DATE, etag: str = "first") -> DailyObjectDescriptor:
    """Build a descriptor with stable metadata for change-detection tests."""
    return DailyObjectDescriptor(run_date, f"days/{run_date}.json", etag, MODIFIED, 100)


def payload(run_date: date = RUN_DATE, **changes: Any) -> bytes:
    """Use the existing website fixture schema with requested publication fields."""
    fixture_path = Path(__file__).parent / "fixtures/days/2026-10-15.json"
    record = json.loads(fixture_path.read_bytes())
    record.update(date=run_date.isoformat(), **changes)
    return json.dumps(record).encode()


async def test_pagination_filters_direct_calendar_dates() -> None:
    """Every page is consumed, but unrelated and malformed keys are ignored."""
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="dummy", aws_secret_access_key="dummy"
    )

    def item(key: str) -> dict[str, Any]:
        """Supply the real SDK response shape required by Stubber."""
        return {"Key": key, "ETag": '"etag"', "LastModified": MODIFIED, "Size": 123}

    try:
        with Stubber(client) as stubber:
            stubber.add_response(
                "list_objects_v2",
                {
                    "Contents": [
                        item("prefix/days/2026-10-27.json"),
                        item("prefix/days/2026-02-30.json"),
                        item("prefix/days/sub/2026-10-26.json"),
                        item("prefix/days/"),
                        item("prefix/days/notes.json"),
                        item("prefix/days/2026-1-01.json"),
                        item("prefix/runs/2026-10-25.json"),
                    ],
                    "IsTruncated": True,
                    "NextContinuationToken": "next",
                },
                {"Bucket": BUCKET, "Prefix": "prefix/days/"},
            )
            stubber.add_response(
                "list_objects_v2",
                {"Contents": [item("prefix/days/2026-10-26.json")], "IsTruncated": False},
                {"Bucket": BUCKET, "Prefix": "prefix/days/", "ContinuationToken": "next"},
            )
            result = await S3DailyListing(client, BUCKET, "/prefix/").list_days()
            assert result == [
                DailyObjectDescriptor(
                    run_date, f"prefix/days/{run_date}.json", '"etag"', MODIFIED, 123
                )
                for run_date in (RUN_DATE, RUN_DATE - timedelta(days=1))
            ]
            stubber.assert_no_pending_responses()
    finally:
        client.close()


async def test_listing_empty_and_access_failure() -> None:
    """An omitted Contents is empty; denied listing never means an empty bucket."""
    paginator = Mock()
    client = Mock(get_paginator=Mock(return_value=paginator))
    paginator.paginate.return_value = iter([{}])
    listing = S3DailyListing(client, BUCKET)
    assert await listing.list_days() == []
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
    paginator.paginate.side_effect = error
    with pytest.raises(ClientError):
        await listing.list_days()


@pytest.mark.parametrize("content", [b"malformed", b"[]", payload(publication_status="pending")])
async def test_hidden_and_invalid_objects_are_not_repeatedly_downloaded(content: bytes) -> None:
    """Unchanged hidden objects reuse retained downloaded state, including invalid JSON."""
    listing = Mock(list_days=AsyncMock(return_value=[descriptor()]))
    reader = Mock(read=AsyncMock(return_value=content))
    cache = S3DailyCache(listing, reader)
    await cache.refresh()
    await cache.refresh()
    assert cache.days() == {}
    reader.read.assert_awaited_once_with(RUN_DATE)


@pytest.mark.parametrize(
    "replacement",
    [
        b"invalid",
        payload(RUN_DATE - timedelta(days=1)),
        payload(publication_status="pending"),
        payload(publication_status="published", spotify_playlist_id=None),
    ],
)
async def test_invalid_or_hidden_replacement_removes_visible_day(replacement: bytes) -> None:
    """A successfully downloaded replacement supersedes a formerly visible object."""
    listing = Mock(list_days=AsyncMock(return_value=[descriptor()]))
    reader = Mock(read=AsyncMock(return_value=payload()))
    cache = S3DailyCache(listing, reader)
    await cache.refresh()
    assert RUN_DATE in cache.days()
    listing.list_days.return_value = [descriptor(etag="changed")]
    reader.read.return_value = replacement
    await cache.refresh()
    assert cache.days() == {}


async def test_pending_to_published_changed_descriptor_and_deletion() -> None:
    """Changed descriptors trigger revalidation; absent keys and missing downloads disappear."""
    listing = Mock(list_days=AsyncMock(return_value=[descriptor()]))
    reader = Mock(read=AsyncMock(return_value=payload(publication_status="pending")))
    cache = S3DailyCache(listing, reader)
    await cache.refresh()
    listing.list_days.return_value = [descriptor(etag="published")]
    reader.read.return_value = payload(publication_status="published", spotify_playlist_id="id")
    await cache.refresh()
    assert cache.days()[RUN_DATE].spotify_playlist_id == "id"
    snapshot = cache.days()
    snapshot.clear()
    assert RUN_DATE in cache.days()
    await cache.refresh()
    assert reader.read.await_count == 2
    listing.list_days.return_value = []
    await cache.refresh()
    assert cache.days() == {}
    listing.list_days.return_value = [descriptor()]
    reader.read.return_value = None
    await cache.refresh()
    assert cache.days() == {}
    reader.read.return_value = payload()
    await cache.refresh()
    assert RUN_DATE in cache.days()


@pytest.mark.parametrize("failure_stage", ["listing", "download"])
async def test_failure_retains_entire_snapshot_and_success_recovers(failure_stage: str) -> None:
    """Failure preserves old visible and hidden state even when other keys changed."""
    listing = Mock(list_days=AsyncMock(return_value=[descriptor()]))
    reader = Mock(read=AsyncMock(return_value=payload()))
    cache = S3DailyCache(listing, reader)
    await cache.refresh()
    old_snapshot = cache.days()
    new_date = RUN_DATE + timedelta(days=1)
    listing.list_days.return_value = [descriptor(new_date)]
    failed_operation = listing.list_days if failure_stage == "listing" else reader.read
    failed_operation.side_effect = OSError("offline")
    with pytest.raises(OSError, match="offline"):
        await cache.refresh()
    assert cache.days() == old_snapshot
    failed_operation.side_effect = None
    reader.read.return_value = payload(new_date)
    await cache.refresh()
    assert list(cache.days()) == [new_date]


async def test_downloads_are_bounded_and_snapshot_swap_is_atomic() -> None:
    """Readers observe no partial results while four downloads execute concurrently."""
    dates = [RUN_DATE + timedelta(days=index) for index in range(9)]
    listing = Mock(list_days=AsyncMock(return_value=[descriptor(day) for day in dates]))
    release = asyncio.Event()
    four_started = asyncio.Event()
    active_count = 0
    peak_count = 0

    async def read(run_date: date) -> bytes:
        """Hold each download so the concurrency bound can be observed."""
        nonlocal active_count, peak_count
        active_count += 1
        peak_count = max(peak_count, active_count)
        if active_count == 4:
            four_started.set()
        await release.wait()
        active_count -= 1
        return payload(run_date)

    cache = S3DailyCache(listing, Mock(read=read))
    refresh_task = asyncio.create_task(cache.refresh())
    await asyncio.wait_for(four_started.wait(), timeout=1)
    assert cache.days() == {}
    release.set()
    await refresh_task
    assert peak_count == 4
    assert set(cache.days()) == set(dates)


async def test_cancellation_drains_sdk_body_before_returning() -> None:
    """Repeated cancellation cannot release an active SDK body or publish its result."""
    started = Event()
    release = Event()
    closed = Event()

    def read() -> bytes:
        """Block in a real adapter worker while leaving the event loop responsive."""
        started.set()
        assert release.wait(3)
        return payload()

    body = Mock(read=read, close=closed.set)
    client = Mock(get_object=Mock(return_value={"Body": body}))
    listing = Mock(list_days=AsyncMock(return_value=[descriptor()]))
    cache = S3DailyCache(listing, S3DailyRecordStore(client, BUCKET))
    refresh_task = asyncio.create_task(cache.refresh())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(2):
            refresh_task.cancel()
            await asyncio.sleep(0)
            assert not refresh_task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    assert closed.is_set()
    assert cache.days() == {}


@pytest.mark.parametrize(
    "changes", [{"etag": "new"}, {"size": 101}, {"last_modified": MODIFIED + timedelta(seconds=1)}]
)
async def test_every_descriptor_metadata_field_invalidates(changes: dict[str, Any]) -> None:
    """A replacement can be detected even if its ETag did not change."""
    original = descriptor()
    listing = Mock(list_days=AsyncMock(return_value=[original]))
    reader = Mock(read=AsyncMock(return_value=payload()))
    cache = S3DailyCache(listing, reader)
    await cache.refresh()
    listing.list_days.return_value = [replace(original, **changes)]
    await cache.refresh()
    assert reader.read.await_count == 2


async def test_download_failure_waits_for_other_active_downloads() -> None:
    """A sibling failure cannot allow client closure while another read is active."""
    other_date = RUN_DATE + timedelta(days=1)
    listing = Mock(list_days=AsyncMock(return_value=[descriptor(), descriptor(other_date)]))
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def read(run_date: date) -> bytes:
        """Fail one task and retain another until explicitly released."""
        if run_date == RUN_DATE:
            raise OSError("download failed")
        blocked.set()
        await release.wait()
        return payload(run_date)

    cache = S3DailyCache(listing, Mock(read=read))
    refresh_task = asyncio.create_task(cache.refresh())
    await asyncio.wait_for(blocked.wait(), 1)
    await asyncio.sleep(0)
    assert not refresh_task.done()
    release.set()
    with pytest.raises(OSError, match="download failed"):
        await refresh_task
    assert cache.days() == {}


async def test_concurrent_refreshes_never_overlap() -> None:
    """Explicit manual refreshes share the same serialization as lifecycle refreshes."""
    started = asyncio.Event()
    release = asyncio.Event()
    listing_count = 0

    async def list_days() -> list[DailyObjectDescriptor]:
        """Hold the first listing while a second refresh waits for ownership."""
        nonlocal listing_count
        listing_count += 1
        started.set()
        await release.wait()
        return []

    cache = S3DailyCache(Mock(list_days=list_days), Mock())
    first_task = asyncio.create_task(cache.refresh())
    await started.wait()
    second_task = asyncio.create_task(cache.refresh())
    await asyncio.sleep(0)
    assert listing_count == 1
    release.set()
    await asyncio.gather(first_task, second_task)
    assert listing_count == 2
