"""SDK limits, read-only filesystem access, and append-only run diagnostics."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel

from the_bot_radio.generation.storage import LocalRunArtifactStore, RunArtifactStore

EventSink = Callable[[dict[str, Any]], None]


@dataclasses.dataclass(frozen=True)
class AgentLimits:
    """Limits for one agent task, excluding input setup and standalone mood scoring."""

    max_turns: int = 40
    budget_usd: float = 5.0
    deadline_seconds: float = 600.0

    def __post_init__(self) -> None:
        """Reject nonpositive and nonfinite resource limits before starting a run."""
        import math

        if self.max_turns < 1 or not all(
            math.isfinite(value) and value > 0 for value in (self.budget_usd, self.deadline_seconds)
        ):
            raise ValueError("agent limits must be finite and positive")


def json_value(value: Any) -> Any:
    """Convert SDK dataclasses and application models to JSON-compatible values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {key: json_value(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, (datetime, date, Path)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace one run artifact with complete JSON, cleaning up on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            json.dump(json_value(payload), output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_diagnostics(artifact_dir: Path, **updates: Any) -> None:
    """Merge progress into the host-only diagnostics file without losing earlier evidence."""
    path = artifact_dir / "diagnostics.json"
    diagnostics = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    diagnostics.update(updates)
    write_json_atomic(path, diagnostics)


class LogTranscript:
    """Append JSONL events with environment credential values redacted."""

    def __init__(self, path: Path, *, console: TextIO | None = None) -> None:
        """Create a log outside the readable snapshot, optionally mirrored to a console."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.console = console
        self.secrets = [
            value
            for key, value in os.environ.items()
            if value and len(value) >= 6 and any(word in key for word in ("TOKEN", "KEY", "SECRET"))
        ]

    def append(self, event: dict[str, Any]) -> None:
        """Write one normalized event without serializing clients or process environments."""
        encoded = json.dumps(json_value(event), ensure_ascii=False)
        for secret in self.secrets:
            encoded = encoded.replace(json.dumps(secret)[1:-1], "[REDACTED]")
        with self.path.open("a", encoding="utf-8") as output_file:
            output_file.write(encoded + "\n")
        if self.console is not None:
            print(encoded, file=self.console, flush=True)


def filesystem_guard(workspace: Path) -> Callable[..., Any]:
    """Build a PreToolUse guard enforcing the agent's workspace boundary.

    The SDK's cwd sets a working directory, not a filesystem access boundary. Read,
    Grep, and Glob could otherwise access files outside the snapshot, including .env
    credentials and sibling run artifacts. Restrict their paths to the workspace and
    reject symlink escapes so only the intended input snapshot is readable.
    """
    workspace_root = workspace.resolve()
    permitted_tools = {
        "Read",
        "Grep",
        "Glob",
        "StructuredOutput",
        "mcp__playlist__search_tracks",
        "mcp__playlist__validate_playlist",
    }

    async def guard(
        hook_input: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Allow intended tools and reject outside paths and symlink escapes."""
        tool_name = hook_input.get("tool_name", "")
        denied_reason: str | None = None
        if tool_name not in permitted_tools:
            denied_reason = "This tool is unavailable in playlist generation."
        elif tool_name in {"Read", "Grep", "Glob"}:
            arguments = hook_input.get("tool_input", {})
            if not isinstance(arguments, dict):
                arguments = {}
                denied_reason = "Tool arguments must be an object."
            path_value = arguments.get("file_path" if tool_name == "Read" else "path")
            if path_value is not None and not isinstance(path_value, str):
                denied_reason = "Snapshot paths must be strings."
            elif tool_name == "Read" and not path_value:
                denied_reason = "A snapshot file path is required."
            elif denied_reason is None:
                requested_path = Path(path_value or ".")
                absolute_path = (
                    requested_path
                    if requested_path.is_absolute()
                    else workspace_root / requested_path
                )
                resolved_path = absolute_path.resolve()
                if not resolved_path.is_relative_to(workspace_root):
                    denied_reason = "Read and search paths must stay inside the run snapshot."
                elif any(parent.is_symlink() for parent in [absolute_path, *absolute_path.parents]):
                    denied_reason = "Symlinks are unavailable in the run snapshot."
            if tool_name == "Glob":
                pattern = str(arguments.get("pattern", ""))
                if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    denied_reason = "Glob patterns must stay inside the run snapshot."
            # A search of a directory must not traverse links to outside data.
            if denied_reason is None and tool_name in {"Grep", "Glob"}:
                if any(path.is_symlink() for path in workspace_root.rglob("*")):
                    denied_reason = "Snapshot searches cannot traverse symlinks."
        if denied_reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": denied_reason,
                }
            }
        return {}

    return guard


class RunRecorder(AbstractAsyncContextManager["RunRecorder"]):
    """Keep local evidence and serialize coalesced artifact uploads for one run.

    Synchronous callbacks belong to the event-loop thread. Explicit checkpoints also
    capture proposal, search evidence, and any available workspace files. ``close``
    only drains/stops workers; call ``finish`` to record a terminal status.
    Local staging and artifact destinations must use separate directories so that
    uploading an older snapshot cannot replace newer staging evidence.
    """

    transcript_interval = 15.0
    write_timeout = 5.0
    close_timeout = 5.0
    retry_delay = 0.1

    def __init__(
        self,
        artifact_store: RunArtifactStore,
        run_date: date,
        run_id: str,
        local_run_dir: Path,
        *,
        console: TextIO | None = None,
    ) -> None:
        """Bind an existing run identity without starting background tasks."""
        self.artifact_store = artifact_store
        self.run_date = run_date
        self.run_id = run_id
        self.local_run_dir = local_run_dir.resolve()
        if isinstance(artifact_store, LocalRunArtifactStore):
            destination = artifact_store.data_dir / "runs" / run_date.isoformat() / run_id
            if self.local_run_dir.is_relative_to(destination) or destination.is_relative_to(
                self.local_run_dir
            ):
                raise ValueError("local run directory must be separate from artifact destination")
        self.transcript = LogTranscript(self.local_run_dir / "transcript.jsonl", console=console)
        self._uploaded: dict[str, bytes] = {}
        self._requested = 0
        self._completed = 0
        self._full_requested = False
        self._wake = asyncio.Event()
        self._checkpoint_result: tuple[int, asyncio.Future[Exception | None]] | None = None
        self._checkpoint_lock = asyncio.Lock()
        self._last_error: Exception | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    async def __aenter__(self) -> RunRecorder:
        """Allow cleanup around preparation or generation exceptions."""
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """Preserve an active primary exception if worker cleanup also fails."""
        try:
            await self.close()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"Run recorder cleanup failed: {type(cleanup_error).__name__}")

    async def start(self, **metadata: Any) -> None:
        """Persist initial diagnostics and the started event before checkpointing."""
        if self._started or self._closed:
            raise RuntimeError("run recorder can only be started once")
        self._started = True
        update_diagnostics(
            self.local_run_dir,
            **{**metadata, "status": "running", "date": self.run_date, "run_id": self.run_id},
        )
        self.transcript.append({**metadata, "event": "started", "date": self.run_date})
        await self._persist(full=False)
        self._worker_task = asyncio.create_task(self._worker(), name="run-artifact-worker")
        self._timer_task = asyncio.create_task(self._periodic(), name="run-artifact-timer")

    def append(self, event: dict[str, Any]) -> None:
        """Append a redacted local event; the timer checkpoints transcript changes."""
        self._require_running()
        self.transcript.append(event)

    def update_diagnostics(self, **updates: Any) -> None:
        """Atomically merge local diagnostics and promptly request an upload."""
        self._require_running()
        update_diagnostics(self.local_run_dir, **updates)
        self._request(full=False)

    def _require_running(self) -> None:
        """Reject callbacks outside an active recorder lifecycle."""
        if not self._started or self._closed:
            raise RuntimeError("run recorder is not running")

    def _request(self, *, full: bool) -> int:
        """Coalesce requests into one pending revision instead of an upload queue."""
        self._requested += 1
        self._full_requested |= full
        self._wake.set()
        return self._requested

    async def checkpoint(self) -> None:
        """Await a complete current snapshot and surface exhausted upload failures."""
        self._require_running()
        if self._worker_task is None:
            raise RuntimeError("run recorder did not finish starting")
        async with self._checkpoint_lock:
            self._require_running()
            requested = self._request(full=True)
            result = asyncio.get_running_loop().create_future()
            self._checkpoint_result = (requested, result)
            error = await result
            if error is not None:
                raise error

    async def _periodic(self) -> None:
        """Request transcript snapshots at a fixed bounded interval."""
        while True:
            await asyncio.sleep(self.transcript_interval)
            self._request(full=False)

    async def _worker(self) -> None:
        """Serialize snapshots and remember failures until the next upload attempt."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            requested = self._requested
            full = self._full_requested
            self._full_requested = False
            try:
                await self._persist(full=full)
            except Exception as error:
                self._last_error = error
            else:
                self._last_error = None
            self._completed = requested
            if self._checkpoint_result is not None:
                revision, result = self._checkpoint_result
                if revision <= requested and not result.done():
                    result.set_result(self._last_error)
            if self._closed and self._completed >= self._requested:
                return

    def _snapshot(self, *, full: bool) -> dict[str, bytes]:
        """Read immutable bytes, allowing absent artifacts but rejecting symlinks."""
        paths = [self.local_run_dir / name for name in ("diagnostics.json", "transcript.jsonl")]
        if full:
            paths.extend(
                self.local_run_dir / name for name in ("proposal.json", "search_results.json")
            )
            workspace = self.local_run_dir / "workspace"
            if workspace.is_symlink():
                raise ValueError("run artifacts must not contain symlinks")
            if workspace.exists():
                paths.extend(workspace.rglob("*"))
        snapshot: dict[str, bytes] = {}
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(self.local_run_dir):
                raise ValueError("run artifacts must not contain symlinks or escape the run")
            if path.is_file():
                snapshot[path.relative_to(self.local_run_dir).as_posix()] = path.read_bytes()
        return snapshot

    async def _persist(self, *, full: bool) -> None:
        """Snapshot first, then upload changed bytes with bounded retry attempts."""
        snapshot = await asyncio.to_thread(self._snapshot, full=full)
        failures: list[Exception] = []
        for name, content in snapshot.items():
            digest = hashlib.sha256(content).digest()
            if self._uploaded.get(name) == digest:
                continue
            for attempt in range(3):
                try:
                    async with asyncio.timeout(self.write_timeout):
                        await self.artifact_store.write(self.run_date, self.run_id, name, content)
                except Exception as error:
                    if attempt == 2:
                        failures.append(error)
                    else:
                        await asyncio.sleep(self.retry_delay * 2**attempt)
                else:
                    self._uploaded[name] = digest
                    break
        if failures:
            raise ExceptionGroup("run artifact writes exhausted retries", failures)

    async def _stop_timer(self) -> None:
        """Cancel and join the periodic requester before finalization."""
        if self._timer_task is not None:
            self._timer_task.cancel()
            await asyncio.gather(self._timer_task, return_exceptions=True)
            self._timer_task = None

    async def finish(self, status: str, **metadata: Any) -> None:
        """Record a terminal outcome, flush all available evidence, and stop workers."""
        self._require_running()
        try:
            await self._stop_timer()
            self.update_diagnostics(**{**metadata, "status": status})
            self.append({**metadata, "event": "finished", "status": status})
            await self.checkpoint()
        except BaseException as primary_error:
            try:
                await self.close()
            except BaseException as cleanup_error:
                primary_error.add_note(
                    f"Run recorder cleanup failed: {type(cleanup_error).__name__}"
                )
            raise
        else:
            await self.close()

    async def close(self) -> None:
        """Bound worker cleanup without creating an outcome or deleting local evidence."""
        if self._closed:
            return
        self._closed = True
        await self._stop_timer()
        if self._worker_task is None:
            return
        worker_task = self._worker_task
        # A sleeping worker can stop immediately; an active one gets time to drain.
        if self._completed >= self._requested:
            worker_task.cancel()
        try:
            done, _ = await asyncio.wait({worker_task}, timeout=self.close_timeout)
            if not done:
                worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        except asyncio.CancelledError:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            raise
        finally:
            self._worker_task = None
            if self._checkpoint_result is not None:
                _, result = self._checkpoint_result
                if not result.done():
                    result.set_result(
                        RuntimeError("run recorder closed before checkpoint completed")
                    )


async def recover_run_artifacts(
    artifact_store: RunArtifactStore,
    run_date: date,
    run_id: str,
    local_run_dir: Path,
) -> None:
    """Upload existing evidence under its original prefix without starting a run."""
    if not local_run_dir.is_dir():
        raise FileNotFoundError(f"original run directory is missing: {local_run_dir}")
    recorder = RunRecorder(artifact_store, run_date, run_id, local_run_dir)
    await recorder._persist(full=True)
