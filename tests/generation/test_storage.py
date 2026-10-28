"""Local storage behavior shared by generation's future remote adapters."""

import asyncio
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from the_bot_radio.generation import storage
from the_bot_radio.generation.storage import (
    LocalDailyRecordStore,
    LocalRunArtifactStore,
    RecordExists,
)

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)


def test_daily_storage_location_does_not_access_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical locations are available before a destination exists or is readable."""
    daily_store = LocalDailyRecordStore(tmp_path / "missing")
    for method in ("exists", "resolve", "is_symlink", "read_bytes", "mkdir"):
        monkeypatch.setattr(Path, method, Mock(side_effect=AssertionError("filesystem access")))
    assert daily_store.storage_location(RUN_DATE) == str(tmp_path / "missing/days/2026-10-27.json")


async def test_daily_store_preserves_bytes_and_never_replaces(tmp_path: Path) -> None:
    """Missing days are optional, but accepted bytes cannot be overwritten."""
    daily_store = LocalDailyRecordStore(tmp_path)
    assert await daily_store.read(RUN_DATE) is None
    original_content = b'{ "original": true }\n'
    location = await daily_store.create(RUN_DATE, original_content)
    assert location == str(tmp_path / "days/2026-10-27.json")
    assert await daily_store.read(RUN_DATE) == original_content
    with pytest.raises(RecordExists):
        await daily_store.create(RUN_DATE, b"replacement")
    assert await daily_store.read(RUN_DATE) == original_content
    assert sorted(path.name for path in (tmp_path / "days").iterdir()) == ["2026-10-27.json"]


async def test_daily_read_propagates_permission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem failures must not silently become missing history."""
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError, match="denied"):
        await LocalDailyRecordStore(tmp_path).read(RUN_DATE)


async def test_failed_creation_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted atomic creation exposes neither partial output nor scratch files."""
    monkeypatch.setattr(storage.os, "link", Mock(side_effect=OSError("link failed")))
    with pytest.raises(OSError, match="link failed"):
        await LocalDailyRecordStore(tmp_path).create(RUN_DATE, b"content")
    assert list((tmp_path / "days").iterdir()) == []


async def test_artifact_snapshots_replace_only_their_run(tmp_path: Path) -> None:
    """Nested workspace artifacts and independent run snapshots share the same layout."""
    artifact_store = LocalRunArtifactStore(tmp_path)
    await artifact_store.write(RUN_DATE, "first", "workspace/history/day.json", b"history")
    await artifact_store.write(RUN_DATE, "first", "diagnostics.json", b"running")
    await artifact_store.write(RUN_DATE, "second", "diagnostics.json", b"skipped")
    location = await artifact_store.write(RUN_DATE, "first", "diagnostics.json", b"accepted")
    assert Path(location).read_bytes() == b"accepted"
    first_run = tmp_path / "runs/2026-10-27/first"
    assert (first_run / "workspace/history/day.json").read_bytes() == b"history"
    assert (first_run.parent / "second/diagnostics.json").read_bytes() == b"skipped"


@pytest.mark.parametrize("run_id", ["", ".", "..", "../outside", "/outside", "a/b"])
async def test_artifact_rejects_invalid_run_id(tmp_path: Path, run_id: str) -> None:
    """Run IDs cannot create or traverse additional directories."""
    with pytest.raises(ValueError, match="run ID"):
        await LocalRunArtifactStore(tmp_path).write(RUN_DATE, run_id, "file", b"content")


@pytest.mark.parametrize(
    "relative_name", ["", ".", "../outside", "/outside", "a/../b", "a//b", "a\\b"]
)
async def test_artifact_rejects_unsafe_name(tmp_path: Path, relative_name: str) -> None:
    """Only relative file names inside the selected run are permitted."""
    with pytest.raises(ValueError, match="artifact name"):
        await LocalRunArtifactStore(tmp_path).write(RUN_DATE, "run", relative_name, b"content")


async def test_stores_reject_symlink_escape(tmp_path: Path) -> None:
    """Existing links cannot redirect daily reads or artifact writes outside the root."""
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "days").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="configured root|symlinks"):
        await LocalDailyRecordStore(root).read(RUN_DATE)
    with pytest.raises(ValueError, match="configured root|symlinks"):
        await LocalDailyRecordStore(root).create(RUN_DATE, b"daily")
    (root / "runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="configured root|symlinks"):
        await LocalRunArtifactStore(root).write(RUN_DATE, "run", "file", b"artifact")
    assert list(outside.iterdir()) == []


async def test_failed_replacement_preserves_snapshot_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement failure leaves the previous complete artifact available."""
    artifact_store = LocalRunArtifactStore(tmp_path)
    location = await artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"original")
    monkeypatch.setattr(storage.os, "replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        await artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"replacement")
    assert Path(location).read_bytes() == b"original"
    assert list(Path(location).parent.iterdir()) == [Path(location)]


async def test_failed_flush_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unflushed bytes never become a daily record or leave scratch files."""
    monkeypatch.setattr(storage.os, "fsync", Mock(side_effect=OSError("flush failed")))
    with pytest.raises(OSError, match="flush failed"):
        await LocalDailyRecordStore(tmp_path).create(RUN_DATE, b"content")
    assert list((tmp_path / "days").iterdir()) == []


async def test_daily_directory_is_a_failed_read(tmp_path: Path) -> None:
    """Only absent paths, rather than invalid file types, count as missing."""
    (tmp_path / "days/2026-10-27.json").mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        await LocalDailyRecordStore(tmp_path).read(RUN_DATE)


async def test_artifact_rejects_links_within_root(tmp_path: Path) -> None:
    """Even links targeting the configured root cannot alias another run."""
    target = tmp_path / "runs/2026-10-27/target"
    target.mkdir(parents=True)
    (target.parent / "alias").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        await LocalRunArtifactStore(tmp_path).write(RUN_DATE, "alias", "file", b"content")


async def test_concurrent_daily_creates_have_one_winner(tmp_path: Path) -> None:
    """Atomic creation accepts exactly one complete payload from competing writers."""
    daily_store = LocalDailyRecordStore(tmp_path)
    payloads = [f"payload-{index}".encode() for index in range(10)]
    results = await asyncio.gather(
        *(daily_store.create(RUN_DATE, payload) for payload in payloads),
        return_exceptions=True,
    )
    winners = [index for index, result in enumerate(results) if isinstance(result, str)]
    assert len(winners) == 1
    assert all(isinstance(result, (str, RecordExists)) for result in results)
    assert await daily_store.read(RUN_DATE) == payloads[winners[0]]
    assert len(list((tmp_path / "days").iterdir())) == 1


async def test_cancelled_artifact_write_joins_filesystem_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation cannot leave an old filesystem replacement racing a newer write."""
    started = Event()
    release = Event()
    original_write = storage.atomic_write_bytes

    def delayed_write(path: Path, content: bytes, *, replace: bool) -> None:
        """Pause one filesystem worker before its atomic replacement."""
        started.set()
        assert release.wait(timeout=2)
        original_write(path, content, replace=replace)

    monkeypatch.setattr(storage, "atomic_write_bytes", delayed_write)
    artifact_store = LocalRunArtifactStore(tmp_path)
    write_task = asyncio.create_task(
        artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"old")
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        write_task.cancel()
        await asyncio.sleep(0)
        assert not write_task.done()
        write_task.cancel()
        await asyncio.sleep(0)
        assert not write_task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await write_task
    location = await artifact_store.write(RUN_DATE, "run", "diagnostics.json", b"new")
    assert Path(location).read_bytes() == b"new"
