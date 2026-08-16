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

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import quote

from intaris.config import EventStoreConfig
from intaris.events.cache import create_event_chunk_cache
from intaris.metrics import Histogram

logger = logging.getLogger(__name__)

# Pattern for validating path components (user_id, session_id).
# Legacy raw path component pattern kept for backward compatibility with
# existing on-disk directories and S3 object prefixes created before opaque
# identifier encoding was introduced.
_LEGACY_SAFE_PATH_COMPONENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:@+-]*$")

# Pattern for parsing chunk filenames: seq_000001_000100.ndjson
_CHUNK_FILENAME = re.compile(r"^seq_(\d{6,})_(\d{6,})\.ndjson$")


def _validate_path_component(value: str, name: str) -> None:
    """Validate an identifier before encoding it into a storage-safe path."""
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > 256:
        raise ValueError(f"{name} too long (max 256 chars)")
    if ".." in value:
        raise ValueError(f"{name} must not contain '..'")
    if "\x00" in value:
        raise ValueError(
            f"{name} contains invalid characters: {value!r}. "
            "Null bytes are not allowed."
        )


def _encode_path_component(value: str, name: str) -> str:
    """Encode an arbitrary identifier into a safe single path component."""
    _validate_path_component(value, name)
    return quote(value, safe="")


def _legacy_path_component(value: str) -> str | None:
    """Return the legacy raw component when it was previously supported."""
    if _LEGACY_SAFE_PATH_COMPONENT.match(value) and ".." not in value:
        return value
    return None


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


def _event_matches_filters(
    event: dict,
    *,
    event_types: set[str] | None = None,
    sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    after_ts: str | None = None,
    before_ts: str | None = None,
) -> bool:
    """Return True when the event matches the requested read filters."""
    event_ts = event.get("ts", "")
    if after_ts and event_ts < after_ts:
        return False
    if before_ts and event_ts > before_ts:
        return False
    if event_types and event.get("type") not in event_types:
        return False
    if sources and event.get("source") not in sources:
        return False
    if exclude_sources and event.get("source") in exclude_sources:
        return False
    return True


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

    def read_seqs(
        self,
        user_id: str,
        session_id: str,
        seqs: set[int],
    ) -> list[dict]:
        """Read events by exact sequence numbers.

        Args:
            seqs: Exact event sequence numbers to return.

        Returns:
            Matching event dicts ordered by seq.
        """
        ...

    def read_before(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read the last matching events with seq < before_seq in chronological order."""
        ...

    def read_tail(
        self,
        user_id: str,
        session_id: str,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        """Read the last matching events in chronological order."""
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

    def available_seq_ranges(
        self,
        user_id: str,
        session_id: str,
        *,
        force_refresh: bool = False,
    ) -> list[tuple[int, int]]:
        """Return available chunk ranges ordered by start sequence."""
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

    def _resolve_under_base(self, *parts: str) -> Path:
        """Resolve a path under the backend base directory."""
        path = (self._base_path.joinpath(*parts)).resolve()
        if not path.is_relative_to(self._base_path.resolve()):
            raise ValueError(
                "Invalid path components: resolved path escapes base directory"
            )
        return path

    def _session_dir_candidates(self, user_id: str, session_id: str) -> list[Path]:
        """Return encoded and legacy session directories, without duplicates."""
        encoded = self._resolve_under_base(
            _encode_path_component(user_id, "user_id"),
            _encode_path_component(session_id, "session_id"),
        )
        candidates = [encoded]
        legacy_user = _legacy_path_component(user_id)
        legacy_session = _legacy_path_component(session_id)
        if legacy_user and legacy_session:
            legacy = self._resolve_under_base(legacy_user, legacy_session)
            if legacy != encoded:
                candidates.append(legacy)
        return candidates

    def _session_dir_for_write(self, user_id: str, session_id: str) -> Path:
        """Return the session directory to write to, preserving legacy paths."""
        candidates = self._session_dir_candidates(user_id, session_id)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _user_dir_candidates(self, user_id: str) -> list[Path]:
        """Return encoded and legacy user directories, without duplicates."""
        encoded = self._resolve_under_base(_encode_path_component(user_id, "user_id"))
        candidates = [encoded]
        legacy_user = _legacy_path_component(user_id)
        if legacy_user:
            legacy = self._resolve_under_base(legacy_user)
            if legacy != encoded:
                candidates.append(legacy)
        return candidates

    def _session_dir(self, user_id: str, session_id: str) -> Path:
        """Resolve and validate the session directory path."""
        return self._session_dir_for_write(user_id, session_id)

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
        session_dir = self._session_dir_for_write(user_id, session_id)
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
        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])

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

    def read_seqs(
        self,
        user_id: str,
        session_id: str,
        seqs: set[int],
    ) -> list[dict]:
        if not seqs:
            return []

        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])

        result: list[dict] = []
        for start_seq, end_seq, chunk_path in chunks:
            if not any(start_seq <= seq <= end_seq for seq in seqs):
                continue
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in events:
                if event.get("seq") in seqs:
                    result.append(event)

        result.sort(key=lambda e: e.get("seq", 0))
        return result

    def read_before(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        if before_seq <= 0 or limit <= 0:
            return []

        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])
        result: list[dict] = []

        for start_seq, _, chunk_path in reversed(chunks):
            if start_seq >= before_seq:
                continue
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in reversed(events):
                if event.get("seq", 0) >= before_seq:
                    continue
                if _event_matches_filters(
                    event,
                    event_types=event_types,
                    sources=sources,
                    exclude_sources=exclude_sources,
                    after_ts=after_ts,
                    before_ts=before_ts,
                ):
                    result.append(event)
                    if len(result) >= limit:
                        result.reverse()
                        return result

        result.reverse()
        return result

    def read_tail(
        self,
        user_id: str,
        session_id: str,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        if limit <= 0:
            return []

        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])
        result: list[dict] = []

        for _, _, chunk_path in reversed(chunks):
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in reversed(events):
                if _event_matches_filters(
                    event,
                    event_types=event_types,
                    sources=sources,
                    exclude_sources=exclude_sources,
                    after_ts=after_ts,
                    before_ts=before_ts,
                ):
                    result.append(event)
                    if len(result) >= limit:
                        result.reverse()
                        return result

        result.reverse()
        return result

    def read_stream(
        self,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
    ) -> Iterator[dict]:
        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])

        for start_seq, end_seq, chunk_path in chunks:
            if end_seq <= after_seq:
                continue
            events = _ndjson_to_events(chunk_path.read_bytes())
            for event in events:
                if event.get("seq", 0) > after_seq:
                    yield event

    def last_seq(self, user_id: str, session_id: str) -> int:
        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda c: c[0])
        if not chunks:
            return 0
        return chunks[-1][1]  # end_seq of last chunk

    def available_seq_ranges(
        self,
        user_id: str,
        session_id: str,
        *,
        force_refresh: bool = False,
    ) -> list[tuple[int, int]]:
        chunks: list[tuple[int, int, Path]] = []
        for session_dir in self._session_dir_candidates(user_id, session_id):
            chunks.extend(self._list_chunks(session_dir))
        chunks.sort(key=lambda chunk: chunk[0])
        return [(start_seq, end_seq) for start_seq, end_seq, _ in chunks]

    def delete_session(self, user_id: str, session_id: str) -> None:
        for session_dir in self._session_dir_candidates(user_id, session_id):
            if session_dir.exists():
                shutil.rmtree(session_dir)

    def delete_all_for_user(self, user_id: str) -> None:
        for user_dir in self._user_dir_candidates(user_id):
            if user_dir.exists():
                shutil.rmtree(user_dir)

    def exists(self, user_id: str, session_id: str) -> bool:
        for session_dir in self._session_dir_candidates(user_id, session_id):
            if session_dir.exists() and self._list_chunks(session_dir):
                return True
        return False


class S3EventBackend:
    """S3/MinIO event storage backend.

    Stores chunked ndjson files as S3 objects:
      s3://{bucket}/events/{user_id}/{session_id}/seq_000001_000100.ndjson
    """

    def __init__(self, config: EventStoreConfig) -> None:
        import boto3
        from botocore.config import Config as BotoConfig
        from botocore.exceptions import ClientError

        kwargs: dict[str, Any] = {
            "endpoint_url": config.s3_endpoint,
            "aws_access_key_id": config.s3_access_key,
            "aws_secret_access_key": config.s3_secret_key,
            "config": BotoConfig(signature_version="s3v4"),
        }
        if config.s3_region:
            kwargs["region_name"] = config.s3_region

        self._client = boto3.client("s3", **kwargs)
        self._client_error_type = ClientError
        self._endpoint_identity = config.s3_endpoint
        self._bucket = config.s3_bucket
        self._chunk_cache_ttl = max(0.0, config.s3_chunk_cache_ttl)
        self._cache_lock = threading.Lock()
        self._chunk_cache: dict[
            tuple[str, str], tuple[float, list[tuple[int, int, str]]]
        ] = {}
        self._object_versions: dict[str, str] = {}
        self._write_prefix_cache: dict[tuple[str, str], str] = {}
        self._chunk_cache_hits = 0
        self._chunk_cache_misses = 0
        self._list_requests = 0
        self._get_requests = 0
        self._put_requests = 0
        self._bytes_read = 0
        self._bytes_written = 0
        self._operation_latency = {
            "list": Histogram(),
            "get": Histogram(),
            "put": Histogram(),
        }
        self._cache_max_entries = 4096
        self._event_cache = create_event_chunk_cache(config)
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
        return (
            f"events/{_encode_path_component(user_id, 'user_id')}/"
            f"{_encode_path_component(session_id, 'session_id')}/"
        )

    def _prefix_candidates(self, user_id: str, session_id: str) -> list[str]:
        """Return encoded and legacy session prefixes, without duplicates."""
        encoded = self._prefix(user_id, session_id)
        candidates = [encoded]
        legacy_user = _legacy_path_component(user_id)
        legacy_session = _legacy_path_component(session_id)
        if legacy_user and legacy_session:
            legacy = f"events/{legacy_user}/{legacy_session}/"
            if legacy != encoded:
                candidates.append(legacy)
        return candidates

    def _user_prefix_candidates(self, user_id: str) -> list[str]:
        """Return encoded and legacy user prefixes, without duplicates."""
        encoded = f"events/{_encode_path_component(user_id, 'user_id')}/"
        candidates = [encoded]
        legacy_user = _legacy_path_component(user_id)
        if legacy_user:
            legacy = f"events/{legacy_user}/"
            if legacy != encoded:
                candidates.append(legacy)
        return candidates

    def _list_chunks(
        self,
        user_id: str,
        session_id: str,
        *,
        force_refresh: bool = False,
    ) -> list[tuple[int, int, str]]:
        """List chunk objects sorted by start sequence.

        Returns list of (start_seq, end_seq, key) tuples.
        """
        cache_key = (user_id, session_id)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._chunk_cache.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and now - cached[0] <= self._chunk_cache_ttl
            ):
                self._chunk_cache_hits += 1
                return list(cached[1])
            self._chunk_cache_misses += 1

        chunks: list[tuple[int, int, str]] = []
        discovered_versions: dict[str, str] = {}
        for prefix in self._prefix_candidates(user_id, session_id):
            continuation_token = None

            while True:
                kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token
                started_at = time.monotonic()
                try:
                    response = self._client.list_objects_v2(**kwargs)
                finally:
                    self._operation_latency["list"].observe(
                        (time.monotonic() - started_at) * 1000
                    )
                    with self._cache_lock:
                        self._list_requests += 1

                for obj in response.get("Contents", []):
                    key = obj["Key"]
                    filename = key.rsplit("/", 1)[-1]
                    parsed = _parse_chunk_filename(filename)
                    if parsed:
                        chunks.append((parsed[0], parsed[1], key))
                        etag = str(obj.get("ETag") or "").strip('"')
                        if etag:
                            discovered_versions[key] = etag

                if not response.get("IsTruncated"):
                    break
                continuation_token = response.get("NextContinuationToken")

        chunks.sort(key=lambda c: c[0])
        with self._cache_lock:
            previous = self._chunk_cache.get(cache_key)
            if previous is not None:
                for _, _, key in previous[1]:
                    self._object_versions.pop(key, None)
            self._chunk_cache[cache_key] = (now, list(chunks))
            self._object_versions.update(discovered_versions)
            self._trim_caches_locked()
        return chunks

    def _trim_caches_locked(self) -> None:
        """Bound process-local metadata caches. Must hold ``_cache_lock``."""
        while len(self._chunk_cache) > self._cache_max_entries:
            _, chunks = self._chunk_cache.pop(next(iter(self._chunk_cache)))
            for _, _, key in chunks:
                self._object_versions.pop(key, None)
        while len(self._write_prefix_cache) > self._cache_max_entries:
            self._write_prefix_cache.pop(next(iter(self._write_prefix_cache)))

    def _write_prefix(self, user_id: str, session_id: str) -> str:
        """Return the stable write prefix, discovering legacy data once."""
        cache_key = (user_id, session_id)
        with self._cache_lock:
            cached = self._write_prefix_cache.get(cache_key)
            if cached is not None:
                return cached

        prefixes = self._prefix_candidates(user_id, session_id)
        prefix = prefixes[0]
        for candidate in prefixes:
            started_at = time.monotonic()
            try:
                response = self._client.list_objects_v2(
                    Bucket=self._bucket, Prefix=candidate, MaxKeys=1
                )
            finally:
                self._operation_latency["list"].observe(
                    (time.monotonic() - started_at) * 1000
                )
                with self._cache_lock:
                    self._list_requests += 1
            if response.get("Contents"):
                prefix = candidate
                break

        with self._cache_lock:
            self._write_prefix_cache[cache_key] = prefix
            self._trim_caches_locked()
        return prefix

    def append(self, user_id: str, session_id: str, events: list[dict]) -> None:
        if not events:
            return
        prefix = self._write_prefix(user_id, session_id)
        start_seq = events[0]["seq"]
        end_seq = events[-1]["seq"]
        filename = _chunk_filename(start_seq, end_seq)
        key = prefix + filename

        body = _events_to_ndjson(events)
        started_at = time.monotonic()
        wrote_object = False
        object_version = ""
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
                IfNoneMatch="*",
            )
            object_version = str(response.get("ETag") or "").strip('"')
            wrote_object = True
        except self._client_error_type as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise
            existing = self._download_object(key)
            if existing != body:
                raise RuntimeError(
                    f"Immutable event chunk collision for s3://{self._bucket}/{key}"
                ) from exc
            object_version = hashlib.sha256(body).hexdigest()
        finally:
            self._operation_latency["put"].observe(
                (time.monotonic() - started_at) * 1000
            )
            with self._cache_lock:
                self._put_requests += 1
        if wrote_object:
            with self._cache_lock:
                self._bytes_written += len(body)
        if not object_version:
            object_version = hashlib.sha256(body).hexdigest()
        with self._cache_lock:
            self._object_versions[key] = object_version
        try:
            self._event_cache.put(
                self._object_cache_key(key, version=object_version),
                body,
            )
        except OSError:
            logger.warning(
                "Could not populate event cache after S3 write", exc_info=True
            )
        cache_key = (user_id, session_id)
        chunk = (start_seq, end_seq, key)
        with self._cache_lock:
            cached = self._chunk_cache.get(cache_key)
            if cached is not None:
                chunks = [item for item in cached[1] if item[2] != key]
                chunks.append(chunk)
                chunks.sort(key=lambda item: item[0])
                self._chunk_cache[cache_key] = (cached[0], chunks)

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
            data = self._get_object(key)
            events = _ndjson_to_events(data)
            for event in events:
                if event.get("seq", 0) > after_seq:
                    result.append(event)
                    if limit and len(result) >= limit:
                        return result
        return result

    def _get_object(self, key: str) -> bytes:
        """Read one immutable object through the configured local cache."""
        return self._event_cache.get_or_load(
            self._object_cache_key(key),
            lambda: self._download_object(key),
        )

    def _download_object(self, key: str) -> bytes:
        """Read one object from S3 and update remote operation metrics."""
        started_at = time.monotonic()
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
        finally:
            self._operation_latency["get"].observe(
                (time.monotonic() - started_at) * 1000
            )
            with self._cache_lock:
                self._get_requests += 1
        with self._cache_lock:
            self._bytes_read += len(data)
        return data

    def _object_cache_key(self, key: str, *, version: str | None = None) -> str:
        if version is None:
            with self._cache_lock:
                version = self._object_versions.get(key, "unversioned")
        return f"{self._endpoint_identity}\0{self._bucket}\0{key}\0{version}"

    def sweep_cache(self) -> int:
        """Run bounded local event cache maintenance."""
        return self._event_cache.sweep()

    def read_seqs(
        self,
        user_id: str,
        session_id: str,
        seqs: set[int],
    ) -> list[dict]:
        if not seqs:
            return []

        chunks = self._list_chunks(user_id, session_id)

        result: list[dict] = []
        for start_seq, end_seq, key in chunks:
            if not any(start_seq <= seq <= end_seq for seq in seqs):
                continue
            data = self._get_object(key)
            events = _ndjson_to_events(data)
            for event in events:
                if event.get("seq") in seqs:
                    result.append(event)

        result.sort(key=lambda e: e.get("seq", 0))
        return result

    def read_before(
        self,
        user_id: str,
        session_id: str,
        before_seq: int,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        if before_seq <= 0 or limit <= 0:
            return []

        chunks = self._list_chunks(user_id, session_id)
        result: list[dict] = []

        for start_seq, _, key in reversed(chunks):
            if start_seq >= before_seq:
                continue
            data = self._get_object(key)
            events = _ndjson_to_events(data)
            for event in reversed(events):
                if event.get("seq", 0) >= before_seq:
                    continue
                if _event_matches_filters(
                    event,
                    event_types=event_types,
                    sources=sources,
                    exclude_sources=exclude_sources,
                    after_ts=after_ts,
                    before_ts=before_ts,
                ):
                    result.append(event)
                    if len(result) >= limit:
                        result.reverse()
                        return result

        result.reverse()
        return result

    def read_tail(
        self,
        user_id: str,
        session_id: str,
        limit: int,
        event_types: set[str] | None = None,
        sources: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        after_ts: str | None = None,
        before_ts: str | None = None,
    ) -> list[dict]:
        if limit <= 0:
            return []

        chunks = self._list_chunks(user_id, session_id)
        result: list[dict] = []

        for _, _, key in reversed(chunks):
            data = self._get_object(key)
            events = _ndjson_to_events(data)
            for event in reversed(events):
                if _event_matches_filters(
                    event,
                    event_types=event_types,
                    sources=sources,
                    exclude_sources=exclude_sources,
                    after_ts=after_ts,
                    before_ts=before_ts,
                ):
                    result.append(event)
                    if len(result) >= limit:
                        result.reverse()
                        return result

        result.reverse()
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
            data = self._get_object(key)
            events = _ndjson_to_events(data)
            for event in events:
                if event.get("seq", 0) > after_seq:
                    yield event

    def last_seq(self, user_id: str, session_id: str) -> int:
        chunks = self._list_chunks(user_id, session_id)
        if not chunks:
            return 0
        return chunks[-1][1]  # end_seq of last chunk

    def available_seq_ranges(
        self,
        user_id: str,
        session_id: str,
        *,
        force_refresh: bool = False,
    ) -> list[tuple[int, int]]:
        return [
            (start_seq, end_seq)
            for start_seq, end_seq, _ in self._list_chunks(
                user_id, session_id, force_refresh=force_refresh
            )
        ]

    def delete_session(self, user_id: str, session_id: str) -> None:
        for prefix in self._prefix_candidates(user_id, session_id):
            self._delete_by_prefix(prefix)
        with self._cache_lock:
            cached = self._chunk_cache.pop((user_id, session_id), None)
            if cached is not None:
                for _, _, key in cached[1]:
                    self._object_versions.pop(key, None)
            self._write_prefix_cache.pop((user_id, session_id), None)

    def delete_all_for_user(self, user_id: str) -> None:
        for prefix in self._user_prefix_candidates(user_id):
            self._delete_by_prefix(prefix)
        with self._cache_lock:
            for key in [key for key in self._chunk_cache if key[0] == user_id]:
                cached = self._chunk_cache.pop(key, None)
                if cached is not None:
                    for _, _, object_key in cached[1]:
                        self._object_versions.pop(object_key, None)
                self._write_prefix_cache.pop(key, None)

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
            started_at = time.monotonic()
            try:
                response = self._client.list_objects_v2(**kwargs)
            finally:
                self._operation_latency["list"].observe(
                    (time.monotonic() - started_at) * 1000
                )
                with self._cache_lock:
                    self._list_requests += 1
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
        for prefix in self._prefix_candidates(user_id, session_id):
            started_at = time.monotonic()
            try:
                response = self._client.list_objects_v2(
                    Bucket=self._bucket, Prefix=prefix, MaxKeys=1
                )
            finally:
                self._operation_latency["list"].observe(
                    (time.monotonic() - started_at) * 1000
                )
                with self._cache_lock:
                    self._list_requests += 1
            if response.get("Contents"):
                return True
        return False

    def metrics(self) -> dict[str, Any]:
        """Return local S3 request, transfer, and cache metrics."""
        with self._cache_lock:
            result = {
                "chunk_cache_hits_total": self._chunk_cache_hits,
                "chunk_cache_misses_total": self._chunk_cache_misses,
                "list_requests_total": self._list_requests,
                "get_requests_total": self._get_requests,
                "put_requests_total": self._put_requests,
                "bytes_read_total": self._bytes_read,
                "bytes_written_total": self._bytes_written,
                "chunk_cache_entries": len(self._chunk_cache),
                "write_prefix_cache_entries": len(self._write_prefix_cache),
            }
        result["operation_latency"] = {
            operation: histogram.snapshot()
            for operation, histogram in self._operation_latency.items()
        }
        result["event_cache"] = self._event_cache.metrics()
        return result
