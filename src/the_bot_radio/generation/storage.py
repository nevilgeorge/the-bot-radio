"""Byte-oriented daily records and run artifacts with a local storage backend."""

import asyncio
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Protocol


async def run_in_thread[T, **P](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Drain a shielded worker before cancellation can release its caller."""
    worker_task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker_task)
    except asyncio.CancelledError:
        # Python cannot stop a running thread. Joining it preserves mutation ordering
        # and prevents client closure while requests or streaming reads are active.
        while not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker_task.cancelled():
            worker_task.exception()
        raise


def validate_artifact_name(run_id: str, relative_name: str) -> Path:
    """Validate shared run identity and relative artifact containment rules."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("run ID must contain only letters, digits, underscores, or hyphens")
    if (
        not relative_name
        or any(unicodedata.category(character) == "Cc" for character in relative_name)
        or "\\" in relative_name
        or any(part in {"", ".", ".."} for part in relative_name.split("/"))
    ):
        raise ValueError("artifact name must be a relative file path within the run")
    return Path(relative_name)


class DailyRecordStore(Protocol):
    """Persist daily bytes without coupling storage to playlist validation.

    Bytes preserve original history payloads and let callers inspect pending or
    legacy records before validating published records as DailyRecord models.
    Serialization and validation belong to callers, keeping local and future
    remote adapters independent of the playlist schema.
    """

    def storage_location(self, run_date: date) -> str:
        """Compute the canonical location without accessing storage or checking existence."""
        ...

    async def read(self, run_date: date) -> bytes | None:
        """Read a day, returning None only for a missing record."""
        ...

    async def write(self, run_date: date, content: bytes) -> str:
        """Create or replace a complete daily record and return its location."""
        ...


class RunArtifactStore(Protocol):
    """Persist complete artifact snapshots beneath a unique run prefix.

    Bytes support varied artifacts (JSON, JSONL transcripts, catalogs, and schemas)
    without requiring storage adapters to parse or reserialize their contents.
    """

    async def write(self, run_date: date, run_id: str, relative_name: str, content: bytes) -> str:
        """Replace an artifact snapshot and return its location."""
        ...


def atomic_write_bytes(path: Path, content: bytes, *, replace: bool) -> None:
    """Atomically expose complete bytes, removing temporary files on every outcome."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output_file:
            temporary_path = Path(output_file.name)
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _contained_path(root: Path, relative_path: Path) -> Path:
    """Reject symlinks and paths escaping an explicitly configured storage root."""
    path = root / relative_path
    if not path.resolve().is_relative_to(root):
        raise ValueError("storage paths must stay inside the configured root")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("storage paths must not contain symlinks")
    return path


class LocalDailyRecordStore:
    """Read and atomically replace daily files under a local data directory."""

    def __init__(self, data_dir: Path) -> None:
        """Resolve the configured root once, independently of the working directory."""
        self.data_dir = data_dir.resolve()

    def storage_location(self, run_date: date) -> str:
        """Compute an absolute canonical path without inspecting the destination."""
        return str(self.data_dir / "days" / f"{run_date.isoformat()}.json")

    async def read(self, run_date: date) -> bytes | None:
        """Read original bytes, propagating errors other than missing files."""
        return await asyncio.to_thread(self._read, run_date)

    def _read(self, run_date: date) -> bytes | None:
        """Read and check the path together in the filesystem worker."""
        path = _contained_path(self.data_dir, Path("days") / f"{run_date.isoformat()}.json")
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def write(self, run_date: date, content: bytes) -> str:
        """Atomically replace daily bytes and drain the worker before cancellation."""
        return await run_in_thread(self._write, run_date, content)

    def _write(self, run_date: date, content: bytes) -> str:
        """Expose complete replacement bytes without removing the previous record first."""
        path = _contained_path(self.data_dir, Path("days") / f"{run_date.isoformat()}.json")
        atomic_write_bytes(path, content, replace=True)
        return str(path)


class LocalRunArtifactStore:
    """Atomically replace artifacts beneath local date and run directories."""

    def __init__(self, data_dir: Path) -> None:
        """Use the same data root and layout as the local daily record store."""
        self.data_dir = data_dir.resolve()

    async def write(self, run_date: date, run_id: str, relative_name: str, content: bytes) -> str:
        """Validate artifact names and write the snapshot outside the event loop."""
        relative_path = validate_artifact_name(run_id, relative_name)
        return await run_in_thread(self._write, run_date, run_id, relative_path, content)

    def _write(self, run_date: date, run_id: str, relative_path: Path, content: bytes) -> str:
        """Check containment and atomically replace a complete artifact."""
        path = _contained_path(
            self.data_dir, Path("runs") / run_date.isoformat() / run_id / relative_path
        )
        atomic_write_bytes(path, content, replace=True)
        return str(path)
