"""Storage backends for session event logs.

Supports two backends:
- Local filesystem for development and single-instance deployments
- S3 (MinIO compatible) for production and multi-instance deployments

Both backends use the same chunked ndjson layout:
  {user_id}/{session_id}/seq_{start:06d}_{end:06d}.ndjson

Each chunk contains one or more events as newline-delimited JSON.
Chunk filenames encode the sequence range for efficient filtering.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Iterator, Protocol

from intaris.config import EventStoreConfig

logger = logging.getLogger(__name__)

# Pattern for validating path components (user_id, session_id).
# Same pattern as mnemory — allows alphanumeric, hyphens, underscores,
# dots, colons, at signs, and forward slashes. Rejects path traversal.
_SAFE_PATH_COMPONENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:@-]*$")

# Pattern for parsing chunk filenames: seq_000001_000100.ndjson
_CHUNK_FILENAME = re.compile(r"^seq_(\d{6,})_(\d{6,})\.ndjson$")


def _validate_path_component(value: str, name: str) -> None:
    """Validate a path component to prevent path traversal attacks."""
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > 256:
        raise ValueError(f"{name} too long (max 256 chars)")
    if ".." in value:
        raise ValueError(f"{name} must not contain '..'")
    if not _SAFE_PATH_COMPONENT.match(value):
        raise ValueError(
            f"{name} contains invalid characters: {value!r}. "
            "Only alphanumeric, hyphens, underscores, dots, colons, "
            "at signs, and forward slashes are allowed."
        )


def _chunk_filename(start_seq: int, end_seq: int) -> str:
    """Generate a chunk filename from a sequence range."""
    return f"seq_{start_seq:06d}_{end_seq:06d}.ndjson"


def _parse_chunk_filename(filename: str) -> tuple[int, int] | None:
    """Parse start and end sequence numbers from a chunk filename.

    Returns (start_seq, end_seq) or None if the filename doesn't match.
    """
    m = _CHUNK_FILENAME.match(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _events_to_ndjson(events: list[dict]) -> bytes:
    """Serialize events to ndjson bytes."""
    lines = []
    for event in events:
        lines.append(json.dumps(event, separators=(",", ":"), sort_keys=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _ndjson_to_events(data: bytes) -> list[dict]:
    """Deserialize ndjson bytes to events."""
    events = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


class EventBackend(Protocol):
    """Protocol for session event storage backends.

    Storage layout (chunked):
      {user_id}/{session_id}/seq_{start:06d}_{end:06d}.ndjson

    All methods are synchronous. Thread safety is the caller's
    responsibility (EventBuffer holds a lock).
    """

    def append(self, user_id: str, session_id: str, events: list[dict]) -> None:
        """Write a chunk of events to storage.

        The events must already have ``seq`` and ``ts`` fields assigned.
        The chunk filename is derived from the first and last event's seq.
        """
        ...

    def read(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
    ) -> list[dict]:
        """Read events, optionally after a sequence number.

        Args:
            after_seq: Return events with seq > this value. 0 = from start.
            limit: Max events to return. 0 = all.

        Returns:
            List of event dicts ordered by seq.
        """
        ...

    def read_stream(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
    ) -> Iterator[dict]:
        """Stream events for large sessions (avoids loading all into memory)."""
        ...

    def last_seq(self, user_id: str, session_id: str) -> int:
        """Get the last sequence number from chunk filenames.

        Returns 0 if no chunks exist.
        """
        ...

    def delete_session(self, user_id: str, session_id: str) -> None:
        """Delete all event chunks for a session."""
        ...

    def delete_all_for_user(self, user_id: str) -> None:
        """Delete all events for a user."""
        ...

    def exists(self, user_id: str, session_id: str) -> bool:
        """Check if any event chunks exist for a session."""
        ...


class FilesystemEventBackend:
    """Local filesystem event storage backend.

    Stores chunked ndjson files under a base directory:
      {base_path}/{user_id}/{session_id}/seq_000001_000100.ndjson
    """

    def __init__(self, config: EventStoreConfig) -> None:
        self._base_path = Path(config.filesystem_path)
        self._base_path.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, user_id: str, session_id: str) -> Path:
        """Resolve and validate the session directory path."""
        _validate_path_component(user_id, "user_id")
        _validate_path_component(session_id, "session_id")
        path = (self._base_path / user_id / session_id).resolve()
        if not path.is_relative_to(self._base_path.resolve()):
            raise ValueError(
                "Invalid path components: resolved path escapes base directory"
            )
        return path

    def _list_chunks(self, session_dir: Path) -> list[tuple[int, int, Path]]:
        """List chunk files sorted by start sequence.

        Returns list of (start_seq, end_seq, path) tuples.
        """
        if not session_dir.exists():
            return []
        chunks = []
        for entry in session_dir.iterdir():
            parsed = _parse_chunk_filename(entry.name)
            if parsed:
                chunks.append((parsed[0], parsed[1], entry))
        chunks.sort(key=lambda c: c[0])
        return chunks

    def append(self, user_id: str, session_id: str, events: list[dict]) -> None:
        if not events:
            return
        session_dir = self._session_dir(user_id, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        start_seq = events[0]["seq"]
        end_seq = events[-1]["seq"]
        filename = _chunk_filename(start_seq, end_seq)
        chunk_path = session_dir / filename

        chunk_path.write_bytes(_events_to_ndjson(events))

    def read(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
    ) -> list[dict]:
        session_dir = self._session_dir(user_id, session_id)
        chunks = self._list_chunks(session_dir)

        result: list[dict] = []
        for start_seq, end_seq, chunk_path in chunks:
            # Skip chunks entirely before after_seq
            if end_seq <= after_seq:
                continue
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in events:
                if event.get("seq", 0) > after_seq:
                    result.append(event)
                    if limit and len(result) >= limit:
                        return result
        return result

    def read_stream(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
    ) -> Iterator[dict]:
        session_dir = self._session_dir(user_id, session_id)
        chunks = self._list_chunks(session_dir)

        for start_seq, end_seq, chunk_path in chunks:
            if end_seq <= after_seq:
                continue
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in events:
                if event.get("seq", 0) > after_seq:
                    yield event

    def last_seq(self, user_id: str, session_id: str) -> int:
        session_dir = self._session_dir(user_id, session_id)
        chunks = self._list_chunks(session_dir)
        if not chunks:
            return 0
        return chunks[-1][1]  # end_seq of last chunk

    def delete_session(self, user_id: str, session_id: str) -> None:
        session_dir = self._session_dir(user_id, session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def delete_all_for_user(self, user_id: str) -> None:
        _validate_path_component(user_id, "user_id")
        user_dir = (self._base_path / user_id).resolve()
        if not user_dir.is_relative_to(self._base_path.resolve()):
            raise ValueError(
                "Invalid path components: resolved path escapes base directory"
            )
        if user_dir.exists():
            shutil.rmtree(user_dir)

    def exists(self, user_id: str, session_id: str) -> bool:
        session_dir = self._session_dir(user_id, session_id)
        if not session_dir.exists():
            return False
        return bool(self._list_chunks(session_dir))


class S3EventBackend:
    """S3/MinIO event storage backend.

    Stores chunked ndjson files as S3 objects:
      s3://{bucket}/events/{user_id}/{session_id}/seq_000001_000100.ndjson
    """

    def __init__(self, config: EventStoreConfig) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        kwargs: dict[str, Any] = {
            "endpoint_url": config.s3_endpoint,
            "aws_access_key_id": config.s3_access_key,
            "aws_secret_access_key": config.s3_secret_key,
            "config": BotoConfig(signature_version="s3v4"),
        }
        if config.s3_region:
            kwargs["region_name"] = config.s3_region

        self._client = boto3.client("s3", **kwargs)
        self._bucket = config.s3_bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Create the bucket if it doesn't exist."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            logger.info("Creating S3 bucket: %s", self._bucket)
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except Exception as e:
                logger.warning("Could not create bucket %s: %s", self._bucket, e)

    def _prefix(self, user_id: str, session_id: str) -> str:
        _validate_path_component(user_id, "user_id")
        _validate_path_component(session_id, "session_id")
        return f"events/{user_id}/{session_id}/"

    def _list_chunks(self, user_id: str, session_id: str) -> list[tuple[int, int, str]]:
        """List chunk objects sorted by start sequence.

        Returns list of (start_seq, end_seq, key) tuples.
        """
        prefix = self._prefix(user_id, session_id)
        chunks: list[tuple[int, int, str]] = []
        continuation_token = None

        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**kwargs)

            for obj in response.get("Contents", []):
                key = obj["Key"]
                filename = key.rsplit("/", 1)[-1]
                parsed = _parse_chunk_filename(filename)
                if parsed:
                    chunks.append((parsed[0], parsed[1], key))

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        chunks.sort(key=lambda c: c[0])
        return chunks

    def append(self, user_id: str, session_id: str, events: list[dict]) -> None:
        if not events:
            return
        prefix = self._prefix(user_id, session_id)
        start_seq = events[0]["seq"]
        end_seq = events[-1]["seq"]
        filename = _chunk_filename(start_seq, end_seq)
        key = prefix + filename

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=_events_to_ndjson(events),
            ContentType="application/x-ndjson",
        )

    def read(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
    ) -> list[dict]:
        chunks = self._list_chunks(user_id, session_id)

        result: list[dict] = []
        for start_seq, end_seq, key in chunks:
            if end_seq <= after_seq:
                continue
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
            events = _ndjson_to_events(data)
            for event in events:
                if event.get("seq", 0) > after_seq:
                    result.append(event)
                    if limit and len(result) >= limit:
                        return result
        return result

    def read_stream(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
    ) -> Iterator[dict]:
        chunks = self._list_chunks(user_id, session_id)

        for start_seq, end_seq, key in chunks:
            if end_seq <= after_seq:
                continue
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
            events = _ndjson_to_events(data)
            for event in events:
                if event.get("seq", 0) > after_seq:
                    yield event

    def last_seq(self, user_id: str, session_id: str) -> int:
        chunks = self._list_chunks(user_id, session_id)
        if not chunks:
            return 0
        return chunks[-1][1]  # end_seq of last chunk

    def delete_session(self, user_id: str, session_id: str) -> None:
        prefix = self._prefix(user_id, session_id)
        self._delete_by_prefix(prefix)

    def delete_all_for_user(self, user_id: str) -> None:
        _validate_path_component(user_id, "user_id")
        prefix = f"events/{user_id}/"
        self._delete_by_prefix(prefix)

    def _delete_by_prefix(self, prefix: str) -> None:
        """Delete all S3 objects under a prefix, respecting the 1000-object batch limit."""
        continuation_token = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,  # Explicit cap matching delete_objects limit
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**kwargs)
            objects = response.get("Contents", [])
            if objects:
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

    def exists(self, user_id: str, session_id: str) -> bool:
        prefix = self._prefix(user_id, session_id)
        response = self._client.list_objects_v2(
            Bucket=self._bucket, Prefix=prefix, MaxKeys=1
        )
        return bool(response.get("Contents"))
