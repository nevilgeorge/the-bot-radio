"""Paginated daily object discovery, separate from date-based storage contracts."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, TypedDict

from .s3_storage import normalize_s3_prefix, validate_s3_bucket
from .storage import run_in_thread


@dataclass(frozen=True)
class DailyObjectDescriptor:
    """Object identity and change metadata from a successful listing."""

    date: date
    key: str
    etag: str
    last_modified: datetime
    size: int


class ListedObject(TypedDict):
    """Required fields of an S3 daily object listing."""

    Key: str
    ETag: str
    LastModified: datetime
    Size: int


class ListingPage(TypedDict, total=False):
    """An S3 page may omit Contents when no objects match."""

    Contents: list[ListedObject]


class ObjectPaginator(Protocol):
    """SDK paginator boundary consumed by discovery."""

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[ListingPage]:
        """Iterate every page under the requested prefix."""
        ...


class ListingClient(Protocol):
    """Listing needs no daily writes or artifact operations."""

    def get_paginator(self, operation_name: str) -> ObjectPaginator:
        """Return the SDK paginator for ListObjectsV2."""
        ...


class DailyListing(Protocol):
    """Discover daily descriptors independently of daily storage."""

    async def list_days(self) -> list[DailyObjectDescriptor]:
        """Return all calendar-valid direct daily objects, or raise."""
        ...


class S3DailyListing:
    """Discover only direct daily JSON keys, with all SDK work off-loop."""

    def __init__(self, client: ListingClient, bucket: str, prefix: str = "") -> None:
        """Retain a caller-owned client and constrain discovery to days/."""
        self.client = client
        self.bucket = validate_s3_bucket(bucket)
        normalized_prefix = normalize_s3_prefix(prefix)
        self.daily_prefix = f"{normalized_prefix}/days/" if normalized_prefix else "days/"

    async def list_days(self) -> list[DailyObjectDescriptor]:
        """Drain all paginator pages before returning any descriptors."""
        return await run_in_thread(self._list_days)

    def _list_days(self) -> list[DailyObjectDescriptor]:
        """Reject unrelated, nested, marker, and impossible date keys."""
        descriptors: list[DailyObjectDescriptor] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.daily_prefix):
            for listed_object in page.get("Contents", []):
                key = listed_object["Key"]
                if not key.startswith(self.daily_prefix):
                    continue
                filename = key[len(self.daily_prefix) :]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", filename, flags=re.ASCII):
                    continue
                try:
                    run_date = date.fromisoformat(filename[:-5])
                except ValueError:
                    continue
                descriptors.append(
                    DailyObjectDescriptor(
                        date=run_date,
                        key=key,
                        etag=listed_object["ETag"],
                        last_modified=listed_object["LastModified"],
                        size=listed_object["Size"],
                    )
                )
        return descriptors
