"""Tests for immutable event chunk caches."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from intaris.events.cache import FilesystemEventChunkCache


def _cache(tmp_path, *, max_bytes=1024 * 1024, ttl_seconds=3600):
    return FilesystemEventChunkCache(
        str(tmp_path / "cache"),
        max_bytes=max_bytes,
        ttl_seconds=ttl_seconds,
        touch_interval_seconds=0,
        sweep_interval_seconds=1,
    )


def test_filesystem_cache_survives_restart(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("bucket\0chunk", b"immutable-data")

    restarted = _cache(tmp_path)

    assert restarted.get("bucket\0chunk") == b"immutable-data"
    assert restarted.metrics()["resident_entries"] == 1


def test_filesystem_cache_evicts_lru_to_size_limit(tmp_path) -> None:
    cache = _cache(tmp_path, max_bytes=200)
    cache.put("first", b"a" * 100)
    cache.put("second", b"b" * 100)

    assert cache.get("first") is None
    assert cache.get("second") == b"b" * 100
    assert cache.metrics()["size_evictions_total"] == 1


def test_filesystem_cache_expires_entries_by_ttl(tmp_path) -> None:
    cache = _cache(tmp_path, ttl_seconds=0.01)
    cache.put("chunk", b"value")
    time.sleep(0.02)

    assert cache.get("chunk") is None
    assert cache.metrics()["ttl_evictions_total"] == 1


def test_filesystem_cache_removes_corrupt_entry(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("chunk", b"value")
    digest = cache._digest_key("chunk")
    cache._path_for_digest(digest).write_bytes(b"corrupt")

    assert cache.get("chunk") is None
    metrics = cache.metrics()
    assert metrics["corruption_recoveries_total"] == 1
    assert metrics["resident_entries"] == 0


def test_get_or_load_coalesces_concurrent_misses(tmp_path) -> None:
    cache = _cache(tmp_path)
    loader_calls = 0
    loader_started = threading.Event()
    release_loader = threading.Event()
    results = []

    def loader() -> bytes:
        nonlocal loader_calls
        loader_calls += 1
        loader_started.set()
        release_loader.wait(timeout=2)
        return b"downloaded"

    def load() -> None:
        results.append(cache.get_or_load("chunk", loader))

    first = threading.Thread(target=load)
    second = threading.Thread(target=load)
    first.start()
    assert loader_started.wait(timeout=1)
    second.start()
    time.sleep(0.01)
    release_loader.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert results == [b"downloaded", b"downloaded"]
    assert loader_calls == 1
    assert cache.metrics()["singleflight_waits_total"] == 1


def test_cache_fill_failure_does_not_fail_authoritative_read(
    tmp_path, monkeypatch
) -> None:
    cache = _cache(tmp_path)

    def fail_replace(source, target) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)

    assert cache.get_or_load("chunk", lambda: b"from-s3") == b"from-s3"
    assert cache.metrics()["write_failures_total"] == 1


def test_startup_removes_orphan_temporary_files(tmp_path) -> None:
    root = tmp_path / "cache"
    shard = root / "ab"
    shard.mkdir(parents=True)
    orphan = shard / ".orphan.cache.1.1.tmp"
    orphan.write_bytes(b"partial")

    cache = _cache(tmp_path)

    assert not orphan.exists()
    assert cache.metrics()["orphan_temp_files_removed_total"] == 1


def test_failed_eviction_keeps_file_in_resident_accounting(
    tmp_path, monkeypatch
) -> None:
    cache = _cache(tmp_path)
    cache.put("chunk", b"value")
    digest = cache._digest_key("chunk")
    path = cache._path_for_digest(digest)
    original_unlink = Path.unlink

    def fail_cache_unlink(self, *args, **kwargs):
        if self == path:
            raise PermissionError("read-only filesystem")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cache_unlink)
    cache._max_bytes = 1

    assert cache.sweep(force=True) == 0
    metrics = cache.metrics()
    assert path.exists()
    assert metrics["resident_entries"] == 1
    assert metrics["resident_bytes"] > 1
    assert metrics["eviction_failures_total"] == 1


def test_cache_files_use_restrictive_permissions(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("chunk", b"value")
    path = cache._path_for_digest(cache._digest_key("chunk"))

    assert cache._root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
