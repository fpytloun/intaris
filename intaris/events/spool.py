"""Single-writer filesystem staging for crash-safe, batched event archival.

Each input batch is an atomically replaced, checksummed file. Export manifests
freeze exact membership before an immutable NDJSON chunk is uploaded. There is
no append-in-place log, database, eviction, or offline-write mode here.
"""

from __future__ import annotations

import fcntl
import hashlib
import heapq
import json
import os
import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from intaris.config import EventStoreConfig
from intaris.events.backend import _file_digest, event_matches_filters
from intaris.events.codec import decode, encode, write_chunk


class SpoolUnavailable(RuntimeError):
    """Writes are closed until backend recovery or local operator intervention."""


def request_digest(events: list[dict], source: str) -> str:
    return hashlib.sha256(
        json.dumps([source, events], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def batch_identity(user_id: str, session_id: str, key: str | None) -> str:
    return hashlib.sha256(
        json.dumps([user_id, session_id, key or uuid.uuid4().hex]).encode()
    ).hexdigest()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class EventSpool:
    """Own pending files, immutable export plans, and write-admission health."""

    def __init__(self, config: EventStoreConfig, backend: Any) -> None:
        if min(config.spool_max_bytes, config.flush_size, config.flush_bytes) <= 0:
            raise ValueError("Spool capacity and batching thresholds must be positive")
        if config.backend_check_interval <= 0 or config.flush_interval <= 0:
            raise ValueError("Backend check and flush intervals must be positive")
        self.config = config
        self.backend = backend
        self.root = Path(config.effective_spool_path).resolve()
        for other in (config.filesystem_path, config.event_cache_path):
            path = Path(other).resolve()
            if self.root.is_relative_to(path) or path.is_relative_to(self.root):
                raise ValueError(
                    "Spool must be separate from archive and cache directories"
                )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._file_lock = (self.root / "owner.lock").open("a+b")
        try:
            fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file_lock.close()
            raise RuntimeError("Event spool already has a writer") from None
        self._lock = threading.RLock()
        self._sessions = tuple(threading.RLock() for _ in range(256))
        self._probe_lock = threading.Lock()
        self._health_lock = threading.RLock()
        self._failed_reads: dict[str, Callable[[], Any]] = {}
        self._read_failure_overflow = False
        self._last_probe = 0.0
        self._backend_error: str | None = None
        self._local_error: str | None = None
        self._closed = False
        self._index: dict[str, dict] = {}
        self._bytes = 0
        try:
            for name in ("batches", "exports", "deletions"):
                (self.root / name).mkdir(exist_ok=True, mode=0o700)
            self._sync_directory(self.root)
            self._sync_directory(self.root.parent)
            identity_path = self.root / "identity.json"
            destination = (
                ["s3", config.s3_endpoint, config.s3_bucket]
                if config.backend == "s3"
                else ["filesystem", str(Path(config.filesystem_path).resolve())]
            )
            if identity_path.exists():
                identity = self._read(identity_path)
                if identity["destination"] != destination or identity["version"] != 1:
                    raise RuntimeError("Spool version or archive destination changed")
            else:
                identity = {
                    "version": 1,
                    "spool_id": uuid.uuid4().hex,
                    "destination": destination,
                }
                self._write(identity_path, identity)
            self.spool_id = identity["spool_id"]
            # Only temporary files can be discarded without inspecting contents.
            for path in self.root.glob("*/*.tmp"):
                path.unlink()
            for path in self.root.glob("batches/*.json"):
                record = self._read(path)
                self._index[record["batch_id"]] = self._metadata(record)
            for path in self.root.glob("exports/*.ndjson"):
                if not path.with_suffix(".json").exists():
                    path.unlink()  # Already confirmed and fully retired.
            # Turning fsync back on also persists surviving files from an
            # unsynced run before recovery can acknowledge those batches.
            if config.spool_fsync:
                directories = [self.root] + [
                    self.root / name for name in ("batches", "exports", "deletions")
                ]
                for directory in directories:
                    for path in directory.iterdir():
                        if path.is_file():
                            with path.open("rb") as stream:
                                self._sync_file(stream.fileno())
                    self._sync_directory(directory)
            self._bytes = sum(
                p.stat().st_size for p in self.root.glob("*/*") if p.is_file()
            )
            self.probe(force=True)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._closed = True
        self._file_lock.close()

    def session_lock(self, key: tuple[str, str]) -> threading.RLock:
        return self._sessions[hash(key) % len(self._sessions)]

    def _check_local(self) -> None:
        if self._closed or self._local_error:
            raise SpoolUnavailable(self._local_error or "Event spool is closed")

    def _sync_file(self, descriptor: int) -> None:
        if self.config.spool_fsync:
            os.fsync(descriptor)

    def _sync_directory(self, path: Path) -> None:
        if self.config.spool_fsync:
            sync_directory(path)

    def _write(self, path: Path, record: dict) -> None:
        payload = encode(record)
        body = hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload
        temporary = path.with_suffix(".tmp")
        with self._lock:
            self._check_local()
            old_size = path.stat().st_size if path.exists() else 0
            try:
                with temporary.open("wb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(body)
                    stream.flush()
                    self._sync_file(stream.fileno())
                os.replace(temporary, path)
                self._sync_directory(path.parent)
                self._bytes += len(body) - old_size
            except OSError as exc:
                self._local_error = f"Spool write failed: {exc}"
                raise SpoolUnavailable(self._local_error) from exc
            finally:
                temporary.unlink(missing_ok=True)

    def _read(self, path: Path) -> dict:
        try:
            digest, payload = path.read_bytes().split(b"\n", 1)
            if hashlib.sha256(payload).hexdigest().encode() != digest:
                raise ValueError("Checksum mismatch")
            return decode(payload)
        except (OSError, ValueError) as exc:
            self._local_error = f"Corrupt or unreadable event spool file: {path.name}"
            raise RuntimeError(self._local_error) from exc

    @staticmethod
    def _metadata(record: dict) -> dict:
        return {k: v for k, v in record.items() if k != "events"}

    def _remove(self, path: Path) -> None:
        with self._lock:
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                self._sync_directory(path.parent)
                self._bytes -= size

    def probe(self, *, force: bool = False) -> None:
        """Probe on admission/maintenance, never clear a latched flush failure."""
        self._check_local()
        with self._probe_lock:
            if (
                not force
                and time.monotonic() - self._last_probe
                < self.config.backend_check_interval
            ):
                return
            try:
                self.backend.probe()
                self._last_probe = time.monotonic()
            except Exception as exc:
                self.backend_failed(exc)
                raise SpoolUnavailable("Event archive unavailable") from exc

    def admit(self) -> None:
        self._check_local()
        if self._backend_error:
            raise SpoolUnavailable("Event archive unavailable; writes paused")
        self.probe()

    def backend_failed(
        self,
        exc: Exception,
        *,
        read_identity: str | None = None,
        retry_read: Callable[[], Any] | None = None,
    ) -> None:
        with self._health_lock:
            self._backend_error = str(exc)
            if read_identity is not None and retry_read is not None:
                if read_identity in self._failed_reads or len(self._failed_reads) < 32:
                    self._failed_reads[read_identity] = retry_read
                else:
                    self._read_failure_overflow = True

    def recovered(self) -> None:
        with self._health_lock:
            if self._read_failure_overflow:
                raise SpoolUnavailable(
                    "Too many failed archive reads; restart after repair"
                )
            for identity, retry_read in list(self._failed_reads.items()):
                try:
                    retry_read()
                except Exception as exc:
                    raise SpoolUnavailable(
                        "Archive read capability is still unavailable"
                    ) from exc
                del self._failed_reads[identity]
            self._backend_error = None

    def check_session(self, key: tuple[str, str]) -> None:
        """A durable deletion intent fences both admission and export."""
        token = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        if (self.root / "deletions" / f"{token}.json").exists():
            raise SpoolUnavailable("Session event deletion is unfinished")

    def metrics(self) -> dict:
        with self._lock:
            oldest = min(
                (
                    datetime.fromisoformat(record["ts"]).timestamp()
                    for record in self._index.values()
                ),
                default=None,
            )
            return {
                "enabled": True,
                "pending_batches": len(self._index),
                "pending_events": sum(r["count"] for r in self._index.values()),
                "pending_sessions": len(
                    {(r["user_id"], r["session_id"]) for r in self._index.values()}
                ),
                "pending_payload_bytes": sum(
                    r["export_bytes"] for r in self._index.values()
                ),
                "oldest_pending_age_seconds": (
                    max(0.0, time.time() - oldest) if oldest is not None else 0.0
                ),
                "disk_bytes": self._bytes,
                "write_admission": not (
                    self._backend_error or self._local_error or self._closed
                ),
                "backend_unavailable": self._backend_error is not None,
                "local_error": self._local_error is not None,
            }

    def sessions(self) -> set[tuple[str, str]]:
        with self._lock:
            keys = {(r["user_id"], r["session_id"]) for r in self._index.values()}
            for path in self.root.glob("exports/*.json"):
                record = self._read(path)
                keys.add((record["user_id"], record["session_id"]))
            return keys

    def records(self, key: tuple[str, str]) -> list[dict]:
        with self._lock:
            return sorted(
                [
                    r.copy()
                    for r in self._index.values()
                    if (r["user_id"], r["session_id"]) == key
                ],
                key=lambda r: (r.get("first_seq", 0), r["ts"], r["batch_id"]),
            )

    def prepare(
        self,
        user_id: str,
        session_id: str,
        events: list[dict],
        source: str,
        batch_id: str,
        *,
        database_backed: bool = False,
    ) -> dict:
        self.admit()
        self.check_session((user_id, session_id))
        digest = request_digest(events, source)
        with self._lock:
            if batch_id in self._index:
                existing = self.load(batch_id)
                if existing["digest"] != digest:
                    raise ValueError("Event batch idempotency payload mismatch")
                return existing
            record = {
                "batch_id": batch_id,
                "user_id": user_id,
                "session_id": session_id,
                "digest": digest,
                "spool_id": self.spool_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "events": events,
                "count": len(events),
                "database_backed": database_backed,
            }
            record["export_bytes"] = sum(
                len(
                    encode(
                        {
                            **event,
                            "seq": 2**63 - 1,
                            "ts": record["ts"],
                            "source": source,
                        }
                    )
                )
                + 1
                for event in events
            )
            size = len(encode(record))
            # Leave space for assignment replacement and sealed upload copies.
            required = 2 * size + record["export_bytes"] + 65536
            reserved_exports = sum(r["export_bytes"] for r in self._index.values())
            if (
                self._bytes + reserved_exports + required > self.config.spool_max_bytes
                or len(self._index) >= 10000
                or shutil.disk_usage(self.root).free < required + 16 * 1024**2
            ):
                raise SpoolUnavailable("Event spool capacity exhausted")
            self._write(self.root / "batches" / f"{batch_id}.json", record)
            record = self.load(batch_id)  # Freeze caller-owned mutable data.
            self._index[batch_id] = self._metadata(record)
            return record

    def load(self, batch_id: str) -> dict:
        record = self._read(self.root / "batches" / f"{batch_id}.json")
        with self._lock:
            assigned = self._index.get(batch_id, {})
            for field in ("first_seq", "last_seq", "ts"):
                if field in assigned:
                    record[field] = assigned[field]
        return record

    def assign(
        self, record: dict, seqs: list[int], *, durable_reservation: bool = False
    ) -> None:
        if seqs != list(range(seqs[0], seqs[0] + record["count"])):
            raise RuntimeError("Spool assignment is not contiguous")
        assigned = {**record, "first_seq": seqs[0], "last_seq": seqs[-1]}
        # Database-backed ingestion already committed this immutable assignment.
        # Recovery joins that reservation to the fsynced input file. Do not
        # rewrite/fsync the whole payload solely to duplicate assignment metadata.
        # Standalone stores have no such reservation and must persist it here.
        if not durable_reservation:
            self._write(self.root / "batches" / f"{record['batch_id']}.json", assigned)
        with self._lock:
            self._index[record["batch_id"]] = self._metadata(assigned)

    @staticmethod
    def enriched(record: dict) -> Iterator[dict]:
        for index, event in enumerate(record["events"]):
            yield {
                **event,
                "seq": record["first_seq"] + index,
                "ts": record["ts"],
                "source": record["source"],
            }

    def events(self, key: tuple[str, str], after_seq: int = 0) -> Iterator[dict]:
        for meta in self.records(key):
            if meta.get("last_seq", 0) <= after_seq:
                continue
            yield from (
                event
                for event in self.enriched(self.load(meta["batch_id"]))
                if event["seq"] > after_seq
            )

    def ranges(self, key: tuple[str, str]) -> list[tuple[int, int]]:
        return [
            (r["first_seq"], r["last_seq"])
            for r in self.records(key)
            if "first_seq" in r
        ]

    def due(self, key: tuple[str, str]) -> bool:
        records = self.records(key)
        return bool(records) and (
            sum(r["count"] for r in records) >= self.config.flush_size
            or sum(
                (self.root / "batches" / f"{r['batch_id']}.json").stat().st_size
                for r in records
            )
            >= self.config.flush_bytes
            or time.time()
            - min(datetime.fromisoformat(r["ts"]).timestamp() for r in records)
            >= self.config.flush_interval
        )

    def export(
        self, key: tuple[str, str], confirmed: Callable[[list[str]], None]
    ) -> tuple[int, int, int]:
        """Retry a frozen chunk before selecting another bounded contiguous batch."""
        self._check_local()
        self.check_session(key)
        chunks = events = payload_bytes = 0
        manifest_path = (
            self.root
            / "exports"
            / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json")
        )
        while True:
            if manifest_path.exists():
                manifest = self._read(manifest_path)
            else:
                selected = []
                size = 0
                count = 0
                for record in self.records(key):
                    if "first_seq" not in record:
                        raise RuntimeError("Unresolved spool assignment blocks export")
                    if selected and record["first_seq"] != selected[-1]["last_seq"] + 1:
                        break
                    selected.append(record)
                    count += record["count"]
                    size += (
                        (self.root / "batches" / f"{record['batch_id']}.json")
                        .stat()
                        .st_size
                    )
                    if (
                        count >= self.config.flush_size
                        or size >= self.config.flush_bytes
                    ):
                        break
                if not selected:
                    return chunks, events, payload_bytes
                manifest = {
                    "user_id": key[0],
                    "session_id": key[1],
                    "batch_ids": [r["batch_id"] for r in selected],
                    "first_seq": selected[0]["first_seq"],
                    "last_seq": selected[-1]["last_seq"],
                }
                try:
                    manifest["destination"] = self.backend.chunk_destination(
                        *key, manifest["first_seq"], manifest["last_seq"]
                    )
                except Exception as exc:
                    self.backend_failed(exc)
                    raise SpoolUnavailable("Event archive unavailable") from exc
                self._write(manifest_path, manifest)
            chunk_path = manifest_path.with_suffix(".ndjson")
            # The source batch files remain intact until the exact upload commits.
            if "digest" not in manifest:
                temporary = chunk_path.with_suffix(".tmp")
                try:
                    with temporary.open("wb") as stream:
                        os.chmod(temporary, 0o600)
                        # Destination is frozen before sealing; legacy manifests
                        # remain uncompressed even if the configured default changed.
                        write_chunk(
                            stream,
                            (
                                event
                                for batch_id in manifest["batch_ids"]
                                for event in self.enriched(self.load(batch_id))
                            ),
                            compression=(
                                "zstd"
                                if manifest["destination"].endswith(".zst")
                                else "none"
                            ),
                        )
                        stream.flush()
                        self._sync_file(stream.fileno())
                    old_size = chunk_path.stat().st_size if chunk_path.exists() else 0
                    os.replace(temporary, chunk_path)
                    self._sync_directory(chunk_path.parent)
                    with self._lock:
                        self._bytes += chunk_path.stat().st_size - old_size
                    manifest["digest"] = _file_digest(chunk_path)
                    self._write(manifest_path, manifest)
                except OSError as exc:
                    self._local_error = f"Spool sealing failed: {exc}"
                    raise SpoolUnavailable(self._local_error) from exc
                finally:
                    temporary.unlink(missing_ok=True)
            if _file_digest(chunk_path) != manifest["digest"]:
                raise RuntimeError("Sealed event chunk checksum mismatch")
            try:
                self.backend.append_file(
                    *key,
                    manifest["first_seq"],
                    manifest["last_seq"],
                    chunk_path,
                    destination=manifest["destination"],
                )
            except Exception as exc:
                self.backend_failed(exc)
                raise SpoolUnavailable(
                    "Event archive upload failed; writes paused"
                ) from exc
            confirmed(manifest["batch_ids"])
            chunks += 1
            events += manifest["last_seq"] - manifest["first_seq"] + 1
            payload_bytes += chunk_path.stat().st_size
            # Keep the manifest until payload cleanup completes. Replaying a
            # partially retired export uses the sealed file, not deleted batches.
            for batch_id in manifest["batch_ids"]:
                self._remove(self.root / "batches" / f"{batch_id}.json")
                with self._lock:
                    self._index.pop(batch_id, None)
            self._remove(manifest_path)
            self._remove(chunk_path)

    def delete(
        self, key: tuple[str, str], finalize: Callable[[tuple[str, str]], None]
    ) -> None:
        """Persist deletion intent before removing any data, including the archive."""
        token = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        tombstone = self.root / "deletions" / f"{token}.json"
        self._write(tombstone, {"user_id": key[0], "session_id": key[1]})
        try:
            self.backend.delete_session(*key)
        except Exception as exc:
            self.backend_failed(exc)
            raise
        finalize(key)
        for record in self.records(key):
            self._remove(self.root / "batches" / f"{record['batch_id']}.json")
            with self._lock:
                self._index.pop(record["batch_id"], None)
        for suffix in (".json", ".ndjson"):
            self._remove(self.root / "exports" / (token + suffix))
        self._remove(tombstone)

    def resume_deletions(self, finalize: Callable[[tuple[str, str]], None]) -> None:
        for path in self.root.glob("deletions/*.json"):
            record = self._read(path)
            key = (record["user_id"], record["session_id"])
            with self.session_lock(key):
                self.delete(key, finalize)


class SpoolReadBackend:
    """Merge archive and pending files under the same per-session lifecycle lock."""

    def __init__(self, spool: EventSpool) -> None:
        self.spool = spool
        self.archive = spool.backend

    def _archive_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.archive, name)(*args, **kwargs)
        except Exception as exc:
            identity = hashlib.sha256(repr((name, args, kwargs)).encode()).hexdigest()
            self.spool.backend_failed(
                exc,
                read_identity=identity,
                retry_read=lambda: getattr(self.archive, name)(*args, **kwargs),
            )
            raise

    @staticmethod
    def _merge(a: list[dict], b: list[dict]) -> list[dict]:
        return sorted({e["seq"]: e for e in a + b}.values(), key=lambda e: e["seq"])

    def read(
        self, user_id: str, session_id: str, after_seq: int = 0, limit: int = 0
    ) -> list[dict]:
        key = (user_id, session_id)
        with self.spool.session_lock(key):
            pending = []
            for event in self.spool.events(key, after_seq):
                pending.append(event)
                if limit and len(pending) >= limit:
                    break
            result = self._merge(
                self._archive_call("read", *key, after_seq, limit), pending
            )
            return result[:limit] if limit else result

    def read_seqs(self, user_id: str, session_id: str, seqs: set[int]) -> list[dict]:
        key = (user_id, session_id)
        with self.spool.session_lock(key):
            pending = [
                e
                for e in self.spool.events(key, min(seqs, default=1) - 1)
                if e["seq"] in seqs
            ]
            return self._merge(self._archive_call("read_seqs", *key, seqs), pending)

    def read_before(
        self, user_id: str, session_id: str, before_seq: int, limit: int, **filters: Any
    ) -> list[dict]:
        key = (user_id, session_id)
        with self.spool.session_lock(key):
            pending = deque(maxlen=limit)
            for event in self.spool.events(key):
                if event["seq"] >= before_seq:
                    break
                if event_matches_filters(event, **filters):
                    pending.append(event)
            return self._merge(
                self._archive_call("read_before", *key, before_seq, limit, **filters),
                list(pending),
            )[-limit:]

    def read_tail(
        self, user_id: str, session_id: str, limit: int, **filters: Any
    ) -> list[dict]:
        return self.read_before(
            user_id,
            session_id,
            self.last_seq(user_id, session_id) + 1,
            limit,
            **filters,
        )

    def read_stream(
        self, user_id: str, session_id: str, after_seq: int = 0
    ) -> Iterator[dict]:
        key = (user_id, session_id)
        # Snapshot pending payloads before releasing the lifecycle lock: export
        # may retire them while a slow stream is consumed. Never hold the lock
        # across a yield, or an HTTP consumer can indefinitely block appends.
        with self.spool.session_lock(key):
            pending = list(self.spool.events(key, after_seq))
            high_water = max(
                self._archive_call("last_seq", *key),
                pending[-1]["seq"] if pending else after_seq,
            )

        def archive_events() -> Iterator[dict]:
            try:
                for event in self.archive.read_stream(*key, after_seq):
                    if event["seq"] > high_water:
                        break
                    yield event
            except Exception as exc:
                identity = hashlib.sha256(
                    repr(("read_stream", key, after_seq)).encode()
                ).hexdigest()
                self.spool.backend_failed(
                    exc,
                    read_identity=identity,
                    retry_read=lambda: deque(
                        self.archive.read_stream(*key, after_seq), maxlen=0
                    ),
                )
                raise

        previous_seq = after_seq
        for event in heapq.merge(
            iter(pending), archive_events(), key=lambda event: event["seq"]
        ):
            if event["seq"] > previous_seq:
                yield event
                previous_seq = event["seq"]

    def available_seq_ranges(
        self, user_id: str, session_id: str, *, force_refresh: bool = False
    ) -> list[tuple[int, int]]:
        key = (user_id, session_id)
        with self.spool.session_lock(key):
            return sorted(
                self.spool.ranges(key)
                + self._archive_call(
                    "available_seq_ranges", *key, force_refresh=force_refresh
                )
            )

    def last_seq(self, user_id: str, session_id: str) -> int:
        return max(
            (end for _, end in self.available_seq_ranges(user_id, session_id)),
            default=0,
        )

    def exists(self, user_id: str, session_id: str) -> bool:
        return bool(self.available_seq_ranges(user_id, session_id))

    def sweep_cache(self) -> None:
        sweep = getattr(self.archive, "sweep_cache", None)
        if sweep:
            sweep()

    def metrics(self) -> dict:
        metrics = getattr(self.archive, "metrics", None)
        return metrics() if metrics else {}
