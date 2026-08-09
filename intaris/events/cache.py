"""Bounded caches for immutable event chunk objects."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from intaris.metrics import Histogram

logger = logging.getLogger(__name__)

_CACHE_HEADER_PREFIX = b"INTARIS-EVENT-CACHE-V1 "
_CACHE_SUFFIX = ".cache"


class EventChunkCache(Protocol):
    """Cache interface for immutable event chunk bytes."""

    def get(self, key: str) -> bytes | None: ...

    def put(self, key: str, value: bytes) -> None: ...

    def get_or_load(self, key: str, loader: Callable[[], bytes]) -> bytes: ...

    def sweep(self, *, force: bool = False) -> int: ...

    def metrics(self) -> dict[str, Any]: ...


class NullEventChunkCache:
    """Disabled cache implementation."""

    def get(self, key: str) -> bytes | None:
        return None

    def put(self, key: str, value: bytes) -> None:
        return None

    def get_or_load(self, key: str, loader: Callable[[], bytes]) -> bytes:
        return loader()

    def sweep(self, *, force: bool = False) -> int:
        return 0

    def metrics(self) -> dict[str, Any]:
        return {"backend": "disabled"}


@dataclass
class _CacheEntry:
    path: Path
    size: int
    accessed_at: float
    touched_at: float


class FilesystemEventChunkCache:
    """Size-bounded filesystem LRU cache for immutable S3 chunk bytes."""

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int,
        ttl_seconds: float,
        touch_interval_seconds: float,
        sweep_interval_seconds: float,
    ) -> None:
        self._root = Path(path).expanduser()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        self._max_bytes = max(1, int(max_bytes))
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._touch_interval_seconds = max(0.0, float(touch_interval_seconds))
        self._sweep_interval_seconds = max(1.0, float(sweep_interval_seconds))
        self._lock = threading.RLock()
        self._key_locks = tuple(threading.Lock() for _ in range(256))
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._resident_bytes = 0
        self._last_sweep_at = 0.0

        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._bytes_served = 0
        self._bytes_stored = 0
        self._ttl_evictions = 0
        self._size_evictions = 0
        self._corruption_recoveries = 0
        self._singleflight_waits = 0
        self._write_failures = 0
        self._eviction_failures = 0
        self._orphan_temp_files_removed = 0
        self._read_latency = Histogram()
        self._fill_latency = Histogram()

        self._load_index()
        self.sweep(force=True)

    @staticmethod
    def _digest_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _path_for_digest(self, digest: str) -> Path:
        return self._root / digest[:2] / f"{digest}{_CACHE_SUFFIX}"

    def _key_lock(self, digest: str) -> threading.Lock:
        return self._key_locks[int(digest[:8], 16) % len(self._key_locks)]

    def _load_index(self) -> None:
        entries: list[tuple[str, _CacheEntry]] = []
        for path in self._root.glob(f"*/*{_CACHE_SUFFIX}"):
            try:
                stat = path.stat()
            except OSError:
                continue
            digest = path.stem
            if len(digest) != 64:
                continue
            entries.append(
                (
                    digest,
                    _CacheEntry(
                        path=path,
                        size=stat.st_size,
                        accessed_at=stat.st_mtime,
                        touched_at=stat.st_mtime,
                    ),
                )
            )
        entries.sort(key=lambda item: item[1].accessed_at)
        with self._lock:
            for digest, entry in entries:
                self._entries[digest] = entry
                self._resident_bytes += entry.size

    def get(self, key: str) -> bytes | None:
        return self._get(key, record=True)

    def _get(self, key: str, *, record: bool) -> bytes | None:
        digest = self._digest_key(key)
        started_at = time.monotonic()
        now = time.time()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                self._record_miss(started_at, record=record)
                return None
            if self._is_expired(entry, now):
                self._remove_locked(digest, reason="ttl")
                self._record_miss(started_at, record=record)
                return None
            try:
                cache_file = entry.path.open("rb")
            except OSError:
                self._remove_locked(digest, unlink=False)
                self._record_miss(started_at, record=record)
                return None
            self._entries.move_to_end(digest)

        try:
            with cache_file:
                payload = self._decode(cache_file.read())
        except (OSError, ValueError):
            with self._lock:
                self._corruption_recoveries += 1
                self._remove_locked(digest)
                if record:
                    self._misses += 1
            self._read_latency.observe((time.monotonic() - started_at) * 1000)
            return None

        with self._lock:
            if record:
                self._hits += 1
                self._bytes_served += len(payload)
            current = self._entries.get(digest)
            if current is not None:
                current.accessed_at = now
                if now - current.touched_at >= self._touch_interval_seconds:
                    try:
                        current.path.touch()
                        current.touched_at = now
                    except OSError:
                        pass
        self._read_latency.observe((time.monotonic() - started_at) * 1000)
        return payload

    def _record_miss(self, started_at: float, *, record: bool) -> None:
        if record:
            self._misses += 1
        self._read_latency.observe((time.monotonic() - started_at) * 1000)

    def put(self, key: str, value: bytes) -> None:
        digest = self._digest_key(key)
        path = self._path_for_digest(digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = self._encode(value)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        started_at = time.monotonic()
        try:
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as cache_file:
                cache_file.write(encoded)
            os.replace(temp_path, path)
        except OSError:
            with self._lock:
                self._write_failures += 1
            raise
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._fill_latency.observe((time.monotonic() - started_at) * 1000)

        now = time.time()
        with self._lock:
            previous = self._entries.pop(digest, None)
            if previous is not None:
                self._resident_bytes -= previous.size
            entry = _CacheEntry(
                path=path,
                size=len(encoded),
                accessed_at=now,
                touched_at=now,
            )
            self._entries[digest] = entry
            self._resident_bytes += entry.size
            self._writes += 1
            self._bytes_stored += len(value)
            self._evict_to_size_locked()

    def get_or_load(self, key: str, loader: Callable[[], bytes]) -> bytes:
        cached = self.get(key)
        if cached is not None:
            return cached

        digest = self._digest_key(key)
        key_lock = self._key_lock(digest)
        acquired = key_lock.acquire(blocking=False)
        if not acquired:
            with self._lock:
                self._singleflight_waits += 1
            key_lock.acquire()
        try:
            cached = self._get(key, record=False)
            if cached is not None:
                return cached
            value = loader()
            try:
                self.put(key, value)
            except OSError:
                logger.warning(
                    "Could not populate filesystem event cache", exc_info=True
                )
            return value
        finally:
            key_lock.release()

    def sweep(self, *, force: bool = False) -> int:
        now = time.time()
        with self._lock:
            if not force and now - self._last_sweep_at < self._sweep_interval_seconds:
                return 0
            self._last_sweep_at = now
            removed = self._remove_orphan_temp_files_locked()
            for digest, entry in list(self._entries.items()):
                if not self._is_expired(entry, now):
                    continue
                if self._remove_locked(digest, reason="ttl"):
                    removed += 1
            removed += self._evict_to_size_locked()
            return removed

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "backend": "filesystem",
                "hits_total": self._hits,
                "misses_total": self._misses,
                "writes_total": self._writes,
                "bytes_served_total": self._bytes_served,
                "bytes_stored_total": self._bytes_stored,
                "resident_entries": len(self._entries),
                "resident_bytes": self._resident_bytes,
                "max_bytes": self._max_bytes,
                "ttl_evictions_total": self._ttl_evictions,
                "size_evictions_total": self._size_evictions,
                "corruption_recoveries_total": self._corruption_recoveries,
                "singleflight_waits_total": self._singleflight_waits,
                "write_failures_total": self._write_failures,
                "eviction_failures_total": self._eviction_failures,
                "orphan_temp_files_removed_total": self._orphan_temp_files_removed,
                "read_latency": self._read_latency.snapshot(),
                "fill_latency": self._fill_latency.snapshot(),
            }

    def _is_expired(self, entry: _CacheEntry, now: float) -> bool:
        return self._ttl_seconds > 0 and now - entry.accessed_at > self._ttl_seconds

    def _evict_to_size_locked(self) -> int:
        removed = 0
        for digest in list(self._entries):
            if self._resident_bytes <= self._max_bytes:
                break
            if self._remove_locked(digest, reason="size"):
                removed += 1
        return removed

    def _remove_locked(
        self,
        digest: str,
        *,
        reason: str | None = None,
        unlink: bool = True,
    ) -> bool:
        entry = self._entries.get(digest)
        if entry is None:
            return False
        if unlink:
            try:
                entry.path.unlink(missing_ok=True)
            except OSError:
                self._eviction_failures += 1
                return False
        self._entries.pop(digest, None)
        self._resident_bytes = max(0, self._resident_bytes - entry.size)
        if reason == "ttl":
            self._ttl_evictions += 1
        elif reason == "size":
            self._size_evictions += 1
        return True

    def _remove_orphan_temp_files_locked(self) -> int:
        removed = 0
        for path in self._root.glob("*/*.tmp"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self._eviction_failures += 1
                continue
            removed += 1
            self._orphan_temp_files_removed += 1
        return removed

    @staticmethod
    def _encode(value: bytes) -> bytes:
        digest = hashlib.sha256(value).hexdigest().encode("ascii")
        return _CACHE_HEADER_PREFIX + digest + b"\n" + value

    @staticmethod
    def _decode(encoded: bytes) -> bytes:
        header, separator, value = encoded.partition(b"\n")
        if not separator or not header.startswith(_CACHE_HEADER_PREFIX):
            raise ValueError("invalid event cache header")
        expected = header[len(_CACHE_HEADER_PREFIX) :]
        actual = hashlib.sha256(value).hexdigest().encode("ascii")
        if expected != actual:
            raise ValueError("event cache checksum mismatch")
        return value


def create_event_chunk_cache(config: Any) -> EventChunkCache:
    """Create the configured cache backend."""
    backend = str(config.event_cache_backend).strip().lower()
    if backend in {"", "disabled", "none"}:
        return NullEventChunkCache()
    if backend == "filesystem":
        try:
            return FilesystemEventChunkCache(
                config.event_cache_path,
                max_bytes=config.event_cache_max_bytes,
                ttl_seconds=config.event_cache_ttl_seconds,
                touch_interval_seconds=config.event_cache_touch_interval_seconds,
                sweep_interval_seconds=config.event_cache_sweep_interval_seconds,
            )
        except OSError:
            logger.warning(
                "Filesystem event cache unavailable; continuing without it",
                exc_info=True,
            )
            return NullEventChunkCache()
    raise ValueError(f"Unsupported event cache backend: {backend}")
