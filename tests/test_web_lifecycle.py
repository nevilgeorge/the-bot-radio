"""Offline application startup, refresh scheduling, and SDK ownership checks."""

import asyncio
import threading
from unittest.mock import Mock

import httpx
import pytest

from the_bot_radio.generation.config import GenerationStorageConfig
from the_bot_radio.web.main import create_app, refresh_daily_cache

pytestmark = pytest.mark.anyio


def selection(data_dir, bucket="example-bucket"):
    """Build explicit configuration without reading developer environment variables."""
    return GenerationStorageConfig(data_dir, bucket, "trial", data_dir / "runs")


def empty_client():
    """Provide an SDK-shaped client that lists no daily objects."""
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [{}]
    return client


async def test_local_startup_never_creates_aws(site_data_dir):
    factory = Mock(side_effect=AssertionError("unexpected AWS initialization"))
    app = create_app(selection(site_data_dir, None), client_factory=factory)
    async with app.router.lifespan_context(app):
        assert app.state.store.latest_day() is not None
    factory.assert_not_called()


async def test_empty_startup_owns_client_off_loop_and_health_contract(site_data_dir):
    client = empty_client()
    loop_thread = threading.get_ident()
    threads = []

    def factory():
        """Record factory execution outside the event loop."""
        threads.append(threading.get_ident())
        return client

    client.close.side_effect = lambda: threads.append(threading.get_ident())
    app = create_app(selection(site_data_dir), client_factory=factory)
    async with app.router.lifespan_context(app):
        assert app.state.store.latest_day() is None
        assert app.state.store.personas()
        assert app.state.store.input_descriptions()
        client.close.assert_not_called()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http_client:
            assert (await http_client.get("/healthz")).json() == {"ok": True, "latest_day": None}
            for path in ("/", "/index", "/bots", "/inputs", "/about"):
                assert (await http_client.get(path)).status_code == 200
    client.close.assert_called_once()
    client.get_object.assert_not_called()
    client.put_object.assert_not_called()
    assert len(threads) == 2
    assert all(thread != loop_thread for thread in threads)


async def test_initial_listing_failure_fails_startup_and_closes(site_data_dir):
    client = empty_client()
    client.get_paginator.return_value.paginate.side_effect = OSError("unavailable")
    app = create_app(selection(site_data_dir), client_factory=lambda: client)
    with pytest.raises(OSError, match="unavailable"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup should fail")
    client.close.assert_called_once()


async def test_refresh_timing_nonoverlap_and_recovery(monkeypatch, caplog):
    intervals = []
    events = []
    active = False

    async def sleep(interval):
        """Record post-attempt waits without relying on wall-clock timing."""
        assert not active
        intervals.append(interval)
        if len(intervals) == 4:
            raise asyncio.CancelledError

    async def refresh():
        """Fail once, recover, then verify that attempts remain serial."""
        nonlocal active
        assert not active
        active = True
        events.append("attempt")
        active = False
        if len(events) == 1:
            raise OSError("offline")

    monkeypatch.setattr("the_bot_radio.web.main.asyncio.sleep", sleep)
    caplog.set_level("INFO")
    cache = Mock(refresh=refresh)
    with pytest.raises(asyncio.CancelledError):
        await refresh_daily_cache(cache, 300)
    assert intervals == [300] * 4
    assert len(events) == 3
    assert "retaining the previous snapshot" in caplog.text
    assert "refresh recovered" in caplog.text


async def test_shutdown_drains_active_listing_before_close(site_data_dir):
    client = empty_client()
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def paginate(**kwargs):
        """Hold the background worker while shutdown requests cancellation."""
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            assert release.wait(5)
        return [{}]

    client.get_paginator.return_value.paginate.side_effect = paginate
    app = create_app(
        selection(site_data_dir), client_factory=lambda: client, refresh_interval=0.001
    )
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    try:
        assert await asyncio.to_thread(started.wait, 5)
        shutdown = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0.01)
        assert not shutdown.done()
        client.close.assert_not_called()
    finally:
        release.set()
    await shutdown
    client.close.assert_called_once()


async def test_cancelled_client_creation_drains_and_closes(site_data_dir):
    client = empty_client()
    started = threading.Event()
    release = threading.Event()

    def factory():
        """Block SDK construction until cancellation reaches lifespan ownership."""
        started.set()
        assert release.wait(5)
        return client

    app = create_app(selection(site_data_dir), client_factory=factory)

    async def startup():
        """Enter startup in a cancellable task."""
        async with app.router.lifespan_context(app):
            pytest.fail("cancelled startup completed")

    task = asyncio.create_task(startup())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        client.close.assert_not_called()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.close.assert_called_once()


async def test_cleanup_preserves_primary_startup_failure(site_data_dir):
    """A failed close must not hide the operation that prevented startup."""
    client = empty_client()
    client.get_paginator.return_value.paginate.side_effect = OSError("listing failed")
    client.close.side_effect = RuntimeError("close failed")
    app = create_app(selection(site_data_dir), client_factory=lambda: client)
    with pytest.raises(OSError, match="listing failed") as failure:
        async with app.router.lifespan_context(app):
            pytest.fail("startup should fail")
    assert failure.value.__notes__ == ["S3 client cleanup failed: RuntimeError"]
