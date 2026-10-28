"""Atomic memory snapshots of S3 daily records, including hidden-object state.

Refreshes perform remote work explicitly. Synchronous consumers only inspect the
last completed snapshot; failed infrastructure never partially changes that snapshot.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import ValidationError

from the_bot_radio.generation.s3_listing import DailyListing, DailyObjectDescriptor

from .models import Day

log = logging.getLogger(__name__)


class DailyReader(Protocol):
    """Read original bytes by date; only confirmed absence returns None."""

    async def read(self, run_date: date) -> bytes | None:
        """Read a daily object without falling back to local data."""
        ...


class DailySnapshotSource(Protocol):
    """Synchronous memory-only source accepted by the website store."""

    def days(self) -> dict[date, Day]:
        """Return the currently visible daily records without remote requests."""
        ...


@dataclass(frozen=True)
class CachedObject:
    """Retain metadata even when downloaded content is invalid or hidden."""

    descriptor: DailyObjectDescriptor
    day: Day | None


class S3DailyCache:
    """Refresh a complete daily snapshot with at most four concurrent downloads."""

    def __init__(self, listing: DailyListing, daily_reader: DailyReader) -> None:
        """Inject remote capabilities; neither dependency is owned by the cache."""
        self.listing = listing
        self.daily_reader = daily_reader
        self._entries: dict[date, CachedObject] = {}
        self._days: dict[date, Day] = {}
        self._refresh_lock = asyncio.Lock()

    def days(self) -> dict[date, Day]:
        """Return an independent mapping of the last successful visible snapshot."""
        return self._days.copy()

    async def refresh(self) -> None:
        """Build separately and atomically replace state only after complete success."""
        async with self._refresh_lock:
            refresh_task = asyncio.create_task(self._assemble())
            try:
                entries = await asyncio.shield(refresh_task)
            except asyncio.CancelledError:
                # SDK threads cannot be stopped. Keep the client owned until every
                # download completes, including after repeated cancellation.
                while not refresh_task.done():
                    try:
                        await asyncio.shield(refresh_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not refresh_task.cancelled():
                    refresh_task.exception()
                raise
            visible_days = {
                run_date: entry.day for run_date, entry in entries.items() if entry.day is not None
            }
            self._entries = entries
            self._days = visible_days

    async def _assemble(self) -> dict[date, CachedObject]:
        """Wait for all downloads even when one fails, preserving client lifetime."""
        descriptors = await self.listing.list_days()
        semaphore = asyncio.Semaphore(4)

        async def load(descriptor: DailyObjectDescriptor) -> CachedObject | None:
            """Reuse unchanged state or fetch and validate one changed object."""
            previous = self._entries.get(descriptor.date)
            if previous is not None and previous.descriptor == descriptor:
                return previous
            async with semaphore:
                content = await self.daily_reader.read(descriptor.date)
            if content is None:
                return None  # Deleted between listing and GetObject.
            try:
                day = Day.model_validate_json(content)
                if day.date != descriptor.date:
                    raise ValueError("daily payload date does not match its key")
                if day.publication_status == "published" and not day.spotify_playlist_id:
                    raise ValueError("published day has no playlist ID")
            except (ValidationError, ValueError) as error:
                log.warning("skipping invalid S3 day %s: %s", descriptor.key, error)
                return CachedObject(descriptor, None)
            return CachedObject(descriptor, None if day.publication_status == "pending" else day)

        results = await asyncio.gather(
            *(load(item) for item in descriptors), return_exceptions=True
        )
        entries: dict[date, CachedObject] = {}
        for result in results:
            if isinstance(result, BaseException):
                raise result
            if result is not None:
                entries[result.descriptor.date] = result
        return entries
