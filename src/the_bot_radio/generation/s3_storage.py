"""Injected, byte-oriented S3 adapters without SDK-owned application retries."""

import re
import unicodedata
from datetime import date
from typing import Protocol, TypedDict

from botocore.exceptions import ClientError

from the_bot_radio.generation.storage import (
    run_in_thread,
    validate_artifact_name,
)


class ObjectBody(Protocol):
    """The streaming body operations consumed by the adapter."""

    def read(self) -> bytes:
        """Read the complete object payload."""
        ...

    def close(self) -> None:
        """Release the underlying connection."""
        ...


class GetObjectResponse(TypedDict):
    """The required subset of an SDK GetObject result."""

    Body: ObjectBody


class S3Client(Protocol):
    """Small SDK boundary implemented by boto3 clients and offline test doubles."""

    def get_object(self, *, Bucket: str, Key: str) -> GetObjectResponse:
        """Fetch a streaming object body."""
        ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> object:
        """Create or replace complete object bytes."""
        ...

    def close(self) -> None:
        """Release client connection pools."""
        ...


def validate_s3_bucket(bucket: str) -> str:
    """Require a general-purpose bucket name rather than a URI, ARN, or path."""
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
        or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket)
    ):
        raise ValueError("S3 bucket must be a bucket name, not a URI, ARN, or path")
    return bucket


def normalize_s3_prefix(prefix: str) -> str:
    """Strip boundary slashes while rejecting ambiguous or unsafe key components."""
    normalized_prefix = prefix.strip("/")
    if (
        "\\" in normalized_prefix
        or any(unicodedata.category(character) == "Cc" for character in normalized_prefix)
        or (
            normalized_prefix
            and any(part in {"", ".", ".."} for part in normalized_prefix.split("/"))
        )
    ):
        raise ValueError("S3 prefix must contain nonempty relative path components")
    return normalized_prefix


class S3DailyRecordStore:
    """Read original daily bytes and replace canonical records unconditionally."""

    def __init__(self, client: S3Client, bucket: str, prefix: str = "") -> None:
        """Retain an injected client; ownership remains with its caller."""
        self.client = client
        self.bucket = validate_s3_bucket(bucket)
        self.prefix = normalize_s3_prefix(prefix)

    def _key(self, run_date: date) -> str:
        """Build a canonical key without accessing S3."""
        return "/".join(filter(None, (self.prefix, "days", f"{run_date.isoformat()}.json")))

    def storage_location(self, run_date: date) -> str:
        """Return the canonical URI without checking existence."""
        return f"s3://{self.bucket}/{self._key(run_date)}"

    async def read(self, run_date: date) -> bytes | None:
        """Read and close the body in a drained worker, mapping only NoSuchKey."""
        return await run_in_thread(self._read, run_date)

    def _read(self, run_date: date) -> bytes | None:
        """Consume the stream before the worker releases the client."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(run_date))
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    async def write(self, run_date: date, content: bytes) -> str:
        """Issue one replacement write and drain it before propagating cancellation."""
        await run_in_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=self._key(run_date),
            Body=content,
            ContentType="application/json",
        )
        return self.storage_location(run_date)


class S3RunArtifactStore:
    """Replace complete artifact snapshots beneath their original run identity."""

    def __init__(self, client: S3Client, bucket: str, prefix: str = "") -> None:
        """Retain an injected client without changing bucket encryption or ACLs."""
        self.client = client
        self.bucket = validate_s3_bucket(bucket)
        self.prefix = normalize_s3_prefix(prefix)

    async def write(self, run_date: date, run_id: str, relative_name: str, content: bytes) -> str:
        """Validate the key and drain replacement writes before cancellation escapes."""
        relative_path = validate_artifact_name(run_id, relative_name)
        key = "/".join(
            filter(None, (self.prefix, "runs", run_date.isoformat(), run_id, relative_name))
        )
        content_type = {
            ".json": "application/json",
            ".jsonl": "application/x-ndjson",
            ".ndjson": "application/x-ndjson",
        }.get(relative_path.suffix.lower(), "application/octet-stream")
        await run_in_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
        return f"s3://{self.bucket}/{key}"
