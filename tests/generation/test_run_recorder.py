"""Standalone recording uses fake stores and never invokes generation services."""

import asyncio
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from the_bot_radio.generation.runtime import RunRecorder, recover_run_artifacts

pytestmark = pytest.mark.anyio
RUN_DATE = date(2026, 10, 27)


class MemoryArtifacts:
    """Capture ordered writes and optionally fail or pause selected snapshots."""

    def __init__(self) -> None:
        self.writes: list[tuple[date, str, str, bytes]] = []
        self.content: dict[str, bytes] = {}
        self.failures = 0
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.active = 0
        self.maximum_active = 0

    async def write(self, run_date: date, run_id: str, name: str, content: bytes) -> str:
        """Record each attempted write, including failures and suspended requests."""
        self.writes.append((run_date, run_id, name, content))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.entered.set()
            if self.block is not None:
                await self.block.wait()
            if self.failures:
                self.failures -= 1
                raise OSError("store unavailable")
            self.content[name] = content
            return f"memory://{run_date}/{run_id}/{name}"
        finally:
            self.active -= 1


def make_recorder(store: MemoryArtifacts, tmp_path: Path) -> RunRecorder:
    """Use short retry delays without changing production timing defaults."""
    recorder = RunRecorder(store, RUN_DATE, "original-run", tmp_path)
    recorder.retry_delay = 0.001
    return recorder


async def wait_diagnostics(store: MemoryArtifacts, **expected: Any) -> None:
    """Wait on observable persistence rather than a fixed scheduling delay."""
    async with asyncio.timeout(1):
        while not all(
            json.loads(store.content.get("diagnostics.json", b"{} ")).get(key) == value
            for key, value in expected.items()
        ):
            await asyncio.sleep(0.001)


async def test_start_finish_persist_and_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial and final snapshots retain metadata while transcript secrets are redacted."""
    monkeypatch.setenv("TEST_API_KEY", "secret-for-recorder")
    store = MemoryArtifacts()
    console = io.StringIO()
    recorder = RunRecorder(store, RUN_DATE, "original-run", tmp_path, console=console)
    await recorder.start(model="fake")
    assert json.loads(store.content["diagnostics.json"])["status"] == "running"
    assert json.loads(store.content["transcript.jsonl"])["event"] == "started"
    recorder.append({"event": "tool", "result": "secret-for-recorder"})
    await recorder.finish("accepted", daily_file="memory://day")
    assert json.loads(store.content["diagnostics.json"])["daily_file"] == "memory://day"
    events = [json.loads(line) for line in store.content["transcript.jsonl"].splitlines()]
    assert events[-1] == {"event": "finished", "status": "accepted", "daily_file": "memory://day"}
    assert events[1]["result"] == "[REDACTED]"
    assert "secret-for-recorder" not in console.getvalue()
    assert recorder._worker_task is None


async def test_diagnostics_prompt_and_transcript_periodic(tmp_path: Path) -> None:
    """Callbacks persist locally immediately and request diagnostics without waiting 15 seconds."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    recorder.transcript_interval = 0.02
    async with recorder:
        await recorder.start()
        recorder.update_diagnostics(inputs=["weather"])
        await wait_diagnostics(store, inputs=["weather"])
        recorder.append({"event": "later"})
        async with asyncio.timeout(1):
            while b"later" not in store.content["transcript.jsonl"]:
                await asyncio.sleep(0.001)
    assert json.loads((tmp_path / "diagnostics.json").read_bytes())["status"] == "running"


async def test_explicit_checkpoint_partial_workspace_and_changed_files(tmp_path: Path) -> None:
    """Available evidence is uploaded verbatim; unchanged successful bytes are skipped."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        history = tmp_path / "workspace/history"
        history.mkdir(parents=True)
        (history / "day.json").write_bytes(b'{ "original": true }\n')
        (tmp_path / "proposal.json").write_bytes(b"proposal")
        await recorder.checkpoint()
        assert store.content["workspace/history/day.json"] == b'{ "original": true }\n'
        assert store.content["proposal.json"] == b"proposal"
        count = len(store.writes)
        await recorder.checkpoint()
        assert len(store.writes) == count
        (tmp_path / "proposal.json").write_bytes(b"revised")
        (tmp_path / "search_results.json").write_bytes(b"search")
        await recorder.checkpoint()
        assert len(store.writes) == count + 2
        assert store.content["proposal.json"] == b"revised"


async def test_slow_writes_are_serialized_and_requests_coalesce(tmp_path: Path) -> None:
    """Newer diagnostics cannot be overwritten by an older in-flight snapshot."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        store.block = asyncio.Event()
        store.entered.clear()
        recorder.update_diagnostics(revision=1)
        await store.entered.wait()
        for revision in range(2, 102):
            recorder.update_diagnostics(revision=revision)
        checkpoint_task = asyncio.create_task(recorder.checkpoint())
        await asyncio.sleep(0)
        store.block.set()
        await checkpoint_task
        assert store.maximum_active == 1
        revisions = [
            json.loads(content)["revision"]
            for _, _, name, content in store.writes
            if name == "diagnostics.json" and "revision" in json.loads(content)
        ]
        assert revisions == [1, 101]


async def test_retries_then_skips_successful_content(tmp_path: Path) -> None:
    """Transient errors get exactly three attempts, then successful bytes are cached."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        (tmp_path / "proposal.json").write_bytes(b"proposal")
        store.failures = 2
        await recorder.checkpoint()
        assert [name for _, _, name, _ in store.writes].count("proposal.json") == 3
        count = len(store.writes)
        await recorder.checkpoint()
        assert len(store.writes) == count


async def test_exhausted_failure_surfaces_and_failed_bytes_retry(tmp_path: Path) -> None:
    """A later checkpoint retries an unchanged artifact whose previous upload failed."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        (tmp_path / "proposal.json").write_bytes(b"proposal")
        store.failures = 3
        with pytest.raises(ExceptionGroup, match="exhausted"):
            await recorder.checkpoint()
        assert "proposal.json" not in store.content
        await recorder.checkpoint()
        assert store.content["proposal.json"] == b"proposal"


async def test_write_timeouts_are_bounded(tmp_path: Path) -> None:
    """A hung store cannot hold an explicit checkpoint indefinitely."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    recorder.write_timeout = 0.01
    async with recorder:
        await recorder.start()
        (tmp_path / "proposal.json").write_bytes(b"proposal")
        store.block = asyncio.Event()
        async with asyncio.timeout(1):
            with pytest.raises(ExceptionGroup) as failure:
                await recorder.checkpoint()
        assert all(isinstance(error, TimeoutError) for error in failure.value.exceptions)
        assert store.active == 0


async def test_finish_failure_preserves_local_terminal_artifacts(tmp_path: Path) -> None:
    """Finalization errors surface after all local terminal evidence has been saved."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    await recorder.start()
    store.failures = 100
    with pytest.raises(ExceptionGroup):
        await recorder.finish("failed", error_type="ValueError")
    assert json.loads((tmp_path / "diagnostics.json").read_bytes())["status"] == "failed"
    assert b'"finished"' in (tmp_path / "transcript.jsonl").read_bytes()
    assert recorder._worker_task is None


async def test_cancel_checkpoint_then_close_cleans_worker(tmp_path: Path) -> None:
    """Cancellation leaves local evidence intact and cleanup bounds the active write."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    recorder.close_timeout = 0.01
    await recorder.start()
    store.block = asyncio.Event()
    store.entered.clear()
    recorder.update_diagnostics(progress="preparing")
    await store.entered.wait()
    checkpoint_task = asyncio.create_task(recorder.checkpoint())
    await asyncio.sleep(0)
    checkpoint_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await checkpoint_task
    await recorder.close()
    assert store.active == 0
    assert recorder._worker_task is None
    assert json.loads((tmp_path / "diagnostics.json").read_bytes())["status"] == "running"


async def test_context_manager_preserves_primary_exception(tmp_path: Path) -> None:
    """An independent cleanup failure cannot mask the generation exception."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)

    async def fail_close() -> None:
        """Simulate an unexpected cleanup failure."""
        raise OSError("cleanup failed")

    recorder.close = fail_close  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="primary") as failure:
        async with recorder:
            raise ValueError("primary")
    assert "OSError" in failure.value.__notes__[0]


async def test_recovery_keeps_original_identity_and_local_bytes(tmp_path: Path) -> None:
    """Recovery performs uploads only, preserving the original status and transcript."""
    (tmp_path / "diagnostics.json").write_bytes(b'{"status":"failed"}')
    (tmp_path / "transcript.jsonl").write_bytes(b'{"event":"original"}\n')
    (tmp_path / "search_results.json").write_bytes(b"search")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    store = MemoryArtifacts()
    await recover_run_artifacts(store, RUN_DATE, "previous-run", tmp_path)
    assert store.content == before
    assert all(day == RUN_DATE and run == "previous-run" for day, run, _, _ in store.writes)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


async def test_checkpoint_rejects_workspace_symlink(tmp_path: Path) -> None:
    """Recovery/checkpoints cannot inadvertently publish files outside the run."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        (tmp_path / "workspace").symlink_to(tmp_path.parent, target_is_directory=True)
        with pytest.raises(ValueError, match="symlinks"):
            await recorder.checkpoint()


async def test_missing_recovery_directory_is_not_created(tmp_path: Path) -> None:
    """Recovery requires existing local evidence and cannot create a fresh run."""
    store = MemoryArtifacts()
    missing_dir = tmp_path / "missing-run"
    with pytest.raises(FileNotFoundError, match="original run directory"):
        await recover_run_artifacts(store, RUN_DATE, "old-run", missing_dir)
    assert not missing_dir.exists()
    assert store.writes == []


async def test_finish_preserves_primary_upload_error_when_cleanup_fails(tmp_path: Path) -> None:
    """Finalization keeps the failed upload as the primary exception."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    await recorder.start()
    original_close = recorder.close

    async def fail_after_close() -> None:
        """Stop workers successfully before simulating another cleanup error."""
        await original_close()
        raise ValueError("cleanup failed")

    recorder.close = fail_after_close  # type: ignore[method-assign]
    store.failures = 100
    with pytest.raises(ExceptionGroup, match="exhausted") as failure:
        await recorder.finish("failed")
    assert "ValueError" in failure.value.__notes__[0]
    assert recorder._worker_task is None


async def test_close_drains_promptly_without_terminal_status(tmp_path: Path) -> None:
    """Closing a pending checkpoint finishes as soon as its current write completes."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    await recorder.start()
    recorder.update_diagnostics(progress="ready")
    async with asyncio.timeout(1):
        await recorder.close()
    assert json.loads(store.content["diagnostics.json"])["progress"] == "ready"
    assert json.loads(store.content["diagnostics.json"])["status"] == "running"


async def test_close_releases_outstanding_checkpoint(tmp_path: Path) -> None:
    """A shutdown timeout releases checkpoint callers along with the upload worker."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    recorder.close_timeout = 0.01
    await recorder.start()
    (tmp_path / "proposal.json").write_bytes(b"proposal")
    store.block = asyncio.Event()
    store.entered.clear()
    checkpoint_task = asyncio.create_task(recorder.checkpoint())
    await store.entered.wait()
    async with asyncio.timeout(1):
        await recorder.close()
        with pytest.raises(RuntimeError, match="closed before checkpoint"):
            await checkpoint_task
    assert store.active == 0


async def test_checkpoint_error_survives_following_partial_success(tmp_path: Path) -> None:
    """A newer diagnostic-only success cannot erase a full checkpoint failure."""
    store = MemoryArtifacts()
    recorder = make_recorder(store, tmp_path)
    async with recorder:
        await recorder.start()
        original_persist = recorder._persist
        partial_completed = asyncio.Event()

        async def controlled_persist(*, full: bool) -> None:
            """Queue a partial success immediately behind one failed full snapshot."""
            if full:
                recorder.update_diagnostics(progress="newer")
                raise OSError("full snapshot failed")
            await original_persist(full=full)
            partial_completed.set()

        recorder._persist = controlled_persist  # type: ignore[method-assign]
        with pytest.raises(OSError, match="full snapshot failed"):
            await recorder.checkpoint()
        await asyncio.wait_for(partial_completed.wait(), timeout=1)
        assert json.loads(store.content["diagnostics.json"])["progress"] == "newer"


def test_local_staging_cannot_alias_artifact_destination(tmp_path: Path) -> None:
    """Old uploaded snapshots must never overwrite newer local recording evidence."""
    from the_bot_radio.generation.storage import LocalRunArtifactStore

    destination = tmp_path / "runs/2026-10-27/original-run"
    with pytest.raises(ValueError, match="separate from artifact destination"):
        RunRecorder(LocalRunArtifactStore(tmp_path), RUN_DATE, "original-run", destination)
    assert not destination.exists()
