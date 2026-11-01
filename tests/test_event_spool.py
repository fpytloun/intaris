"""Crash, archive-availability, and lifecycle contracts for the local spool."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from intaris.config import DBConfig, EventStoreConfig
from intaris.db import Database
from intaris.events.backend import FilesystemEventBackend
from intaris.events.spool import SpoolUnavailable
from intaris.events.store import EventStore
from intaris.session import SessionStore

EVENT = {"type": "user_message", "data": {"content": "Inspect the repository"}}


@pytest.fixture(params=["none", "zstd"])
def config(tmp_path, request):
    return EventStoreConfig(
        backend="filesystem",
        filesystem_path=str(tmp_path / "archive"),
        spool_path=str(tmp_path / "spool"),
        compression=request.param,
        flush_size=100,
    )


@pytest.fixture(params=["sqlite", "postgresql"])
def sessions(tmp_path, request, monkeypatch):
    if request.param == "postgresql":
        dsn = os.environ.get("INTARIS_SPOOL_TEST_DSN")
        if not dsn:
            pytest.skip("Disposable PostgreSQL DSN not configured")
        db = Database(DBConfig(backend="postgresql", database_url=dsn))
        with db.cursor() as cur:
            cur.execute("TRUNCATE sessions CASCADE")
        monkeypatch.setenv("INTARIS_SPOOL_WORKER_DSN", dsn)
    else:
        db = Database(DBConfig(path=str(tmp_path / "db.sqlite")))
        monkeypatch.delenv("INTARIS_SPOOL_WORKER_DSN", raising=False)
    sessions = SessionStore(db)
    sessions.create(session_id="recording", user_id="tenant", intention="Test spool")
    yield sessions
    db.close()


@pytest.fixture
def store(config, sessions):
    store = EventStore(config)
    store.set_session_store(sessions)
    store.recover_spool()
    yield store
    store.close()


def test_default_enabled_pending_reads_and_batched_archive(store, config):
    assert config.spool_enabled is True
    for i in range(5):
        assert store.append(
            "tenant", "recording", [EVENT], idempotency_key=f"batch-{i}"
        ) == [i + 1]
    assert not list(Path(config.filesystem_path).rglob("*.ndjson*"))
    assert store._buffers == {}
    assert store.last_seq("tenant", "recording") == 5
    assert store.availability("tenant", "recording").history_gap is None
    assert [e["seq"] for e in store.read("tenant", "recording")] == list(range(1, 6))
    assert [e["seq"] for e in store.read_seqs("tenant", "recording", {2, 4})] == [2, 4]
    assert [e["seq"] for e in store.read_tail("tenant", "recording", 2)] == [4, 5]
    assert [e["seq"] for e in store.read_stream("tenant", "recording")] == list(
        range(1, 6)
    )
    store.flush_all()
    chunks = list(Path(config.filesystem_path).rglob("*.ndjson*"))
    assert len(chunks) == 1
    assert chunks[0].name.endswith(
        ".ndjson.zst" if config.compression == "zstd" else ".ndjson"
    )
    assert store.buffered_event_count == 0
    assert store.metrics()["spool"]["disk_bytes"] >= 0
    assert store.append("tenant", "recording", [EVENT], idempotency_key="batch-0") == [
        1
    ]
    assert store.last_seq("tenant", "recording") == 5


def test_failed_archive_stops_new_writes_and_recovery_preserves_original_chunk(
    store, config, monkeypatch
):
    archive = store._spool.backend
    original = archive.append_file
    uploaded = []

    def lost_response(*args, **kwargs):
        uploaded.append(Path(args[-1]).read_bytes())
        original(*args, **kwargs)
        raise OSError("Lost archive response")

    store.append("tenant", "recording", [EVENT], idempotency_key="first")
    monkeypatch.setattr(archive, "append_file", lost_response)
    with pytest.raises(SpoolUnavailable):
        store.flush_all()
    with pytest.raises(SpoolUnavailable):
        store.append("tenant", "recording", [EVENT], idempotency_key="second")
    assert store.buffered_event_count == 1
    monkeypatch.setattr(archive, "append_file", original)
    store.flush_all()
    assert store.buffered_event_count == 0
    assert (
        next(Path(config.filesystem_path).rglob("*.ndjson*")).read_bytes()
        == uploaded[0]
    )
    assert store.append("tenant", "recording", [EVENT], idempotency_key="second") == [2]


def test_database_assignment_does_not_rewrite_durable_payload(
    store, config, monkeypatch
):
    original = store._spool._write
    writes = []

    def count_writes(path, record):
        if path.parent.name == "batches":
            writes.append(record.copy())
        return original(path, record)

    monkeypatch.setattr(store._spool, "_write", count_writes)
    assert store.append("tenant", "recording", [EVENT]) == [1]
    assert len(writes) == 1
    assert "first_seq" not in writes[0]
    assert store.read("tenant", "recording")[0]["seq"] == 1


def test_database_backed_recovery_cannot_silently_use_local_allocator(store, config):
    store.append("tenant", "recording", [EVENT])
    store.close()
    reopened = EventStore(config)
    try:
        with pytest.raises(RuntimeError, match="requires SessionStore"):
            reopened.recover_spool()
    finally:
        reopened.close()


def test_probe_failure_blocks_before_sequence_allocation(store, sessions, monkeypatch):
    store._spool._last_probe = 0
    monkeypatch.setattr(
        store._spool.backend,
        "probe",
        lambda: (_ for _ in ()).throw(OSError("Archive down")),
    )
    with pytest.raises(SpoolUnavailable):
        store.append("tenant", "recording", [EVENT])
    assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 0
    assert store.buffered_event_count == 0


def test_disk_failure_does_not_allocate_or_fall_back_to_memory(
    store, sessions, monkeypatch
):
    import intaris.events.spool as spool_module

    monkeypatch.setattr(
        spool_module.os,
        "fsync",
        lambda *args: (_ for _ in ()).throw(OSError("Disk full")),
    )
    with pytest.raises(SpoolUnavailable):
        store.append("tenant", "recording", [EVENT])
    assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 0
    assert store._buffers == {}


def test_capacity_rejects_before_reservation(store, sessions):
    store._spool.config.spool_max_bytes = 1024
    with pytest.raises(SpoolUnavailable, match="capacity"):
        store.append("tenant", "recording", [EVENT])
    assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 0


def test_duplicate_concurrency_and_digest_conflict(store, sessions):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: store.append(
                    "tenant", "recording", [EVENT], idempotency_key="same"
                ),
                range(20),
            )
        )
    assert results == [[1]] * 20
    assert store.buffered_event_count == 1
    assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 1
    with pytest.raises(ValueError, match="mismatch"):
        store.append(
            "tenant",
            "recording",
            [{"type": "user_message", "data": {"content": "Different"}}],
            idempotency_key="same",
        )


def test_lock_disabled_pending_and_corrupt_file_fail_closed(store, config, sessions):
    with pytest.raises(RuntimeError, match="writer"):
        EventStore(config)
    store.append("tenant", "recording", [EVENT])
    with pytest.raises(RuntimeError, match="drain"):
        EventStore(replace(config, spool_enabled=False))
    store.close()
    path = next(Path(config.spool_path).glob("batches/*.json"))
    path.write_bytes(b"bad-checksum\n{}")
    with pytest.raises(RuntimeError, match="Corrupt"):
        EventStore(config)


def test_delete_does_not_replay_and_same_key_is_rejected(store, sessions, config):
    store.append("tenant", "recording", [EVENT], idempotency_key="same")
    store.delete_session("tenant", "recording")
    store.close()
    reopened = EventStore(config)
    try:
        reopened.set_session_store(sessions)
        reopened.recover_spool()
        assert reopened.read("tenant", "recording") == []
        with pytest.raises(ValueError, match="deleted"):
            reopened.append("tenant", "recording", [EVENT], idempotency_key="same")
    finally:
        reopened.close()


def test_unavailable_archive_rejects_startup(config, monkeypatch):
    monkeypatch.setattr(
        FilesystemEventBackend,
        "probe",
        lambda *args: (_ for _ in ()).throw(OSError("Unavailable")),
    )
    with pytest.raises(SpoolUnavailable):
        EventStore(config)


def test_api_idempotency_conflict_and_backend_gate(store, sessions, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import intaris.server as server
    from intaris.api.deps import SessionContext, get_session_context
    from intaris.api.events import router

    monkeypatch.setattr(server, "_get_db", lambda: sessions._db)
    app = FastAPI()
    app.state.event_store = store
    app.dependency_overrides[get_session_context] = lambda: SessionContext(
        user_id="tenant", agent_id="worker", user_bound=True
    )
    app.include_router(router)
    with TestClient(app) as client:
        url = "/session/recording/events?idempotency_key=recording:retry"
        assert client.post(url, json=EVENT).json()["first_seq"] == 1
        assert client.post(url, json=EVENT).json()["first_seq"] == 1
        conflict = client.post(url, json={"type": "message", "data": {}})
        assert conflict.status_code == 409
        store._spool.backend_failed(OSError("backend down"))
        assert client.post(url, json=EVENT).status_code == 503
        assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 1


def test_replaced_spool_does_not_hide_missing_payload(store, config, sessions):
    store.append("tenant", "recording", [EVENT])
    store.close()
    replacement = EventStore(
        replace(config, spool_path=config.spool_path + "-replacement")
    )
    try:
        replacement.set_session_store(sessions)
        with pytest.raises(RuntimeError, match="Missing event spool"):
            replacement.recover_spool()
    finally:
        replacement.close()


def test_deletion_resumes_after_archive_response_loss(
    store, config, sessions, monkeypatch
):
    store.append("tenant", "recording", [EVENT])
    store.flush_all()
    store.append("tenant", "recording", [EVENT])
    original = store._spool.backend.delete_session

    def interrupted(*args):
        original(*args)
        raise OSError("Lost deletion response")

    monkeypatch.setattr(store._spool.backend, "delete_session", interrupted)
    with pytest.raises(OSError):
        store.delete_session("tenant", "recording")
    with pytest.raises(SpoolUnavailable):
        store.append("tenant", "recording", [EVENT], idempotency_key="after-delete")
    with pytest.raises(SpoolUnavailable, match="deletion"):
        store.flush_session("tenant", "recording")
    assert sessions.get("recording", user_id="tenant")["last_event_seq"] == 2
    store.close()
    reopened = EventStore(config)
    try:
        reopened.set_session_store(sessions)
        reopened.recover_spool()
        assert reopened.read("tenant", "recording") == []
        assert not list(Path(config.spool_path).glob("deletions/*.json"))
    finally:
        reopened.close()


def test_healthy_probe_cannot_clear_failed_read(store, monkeypatch):
    store.append("tenant", "recording", [EVENT])
    store.flush_all()
    archive = store._spool.backend
    original = archive.read

    def unreadable(*args, **kwargs):
        raise PermissionError("GetObject denied while HeadBucket succeeds")

    monkeypatch.setattr(archive, "read", unreadable)
    with pytest.raises(PermissionError):
        store.read("tenant", "recording")
    with pytest.raises(SpoolUnavailable, match="read capability"):
        store.flush_all()
    with pytest.raises(SpoolUnavailable):
        store.append("tenant", "recording", [EVENT])
    monkeypatch.setattr(archive, "read", original)
    store.flush_all()
    assert store.append("tenant", "recording", [EVENT]) == [2]


def test_api_receipt_survives_drain_and_disable(store, config, sessions, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import intaris.server as server
    from intaris.api.deps import SessionContext, get_session_context
    from intaris.api.events import router

    store.append(
        "tenant", "recording", [EVENT], source="client", idempotency_key="recording:key"
    )
    store.flush_all()
    store.close()
    disabled = EventStore(replace(config, spool_enabled=False))
    disabled.set_session_store(sessions)
    disabled.recover_spool()
    monkeypatch.setattr(server, "_get_db", lambda: sessions._db)
    app = FastAPI()
    app.state.event_store = disabled
    app.dependency_overrides[get_session_context] = lambda: SessionContext(
        user_id="tenant", agent_id="worker", user_bound=True
    )
    app.include_router(router)
    with TestClient(app) as client:
        url = "/session/recording/events?idempotency_key=recording:key"
        response = client.post(url, json=EVENT)
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "count": 1,
            "first_seq": 1,
            "last_seq": 1,
        }
        assert client.post(url, json={"type": "message", "data": {}}).status_code == 409
    row = sessions.get("recording", user_id="tenant")
    assert row["last_event_seq"] == 1
    assert row["state_revision"] == 2


def test_count_and_age_batching_are_preserved(config):
    config.flush_size = 2
    store = EventStore(config)
    try:
        store.append("tenant", "recording", [EVENT])
        assert not list(Path(config.filesystem_path).rglob("*.ndjson*"))
        store.append("tenant", "recording", [EVENT])
        assert len(list(Path(config.filesystem_path).rglob("*.ndjson*"))) == 1
        store.append("tenant", "recording", [EVENT])
        record = store._spool.records(("tenant", "recording"))[0]
        # Exercise persisted age, not the legacy in-memory timer.
        record = store._spool.load(record["batch_id"])
        record["ts"] = "2020-01-01T00:00:00+00:00"
        store._spool._write(
            Path(config.spool_path) / "batches" / f"{record['batch_id']}.json", record
        )
        store._spool._index[record["batch_id"]] = store._spool._metadata(record)
        assert store.flush_stale() == 1
    finally:
        store.close()


def test_s3_streaming_upload_retry_over_http(config, sessions, monkeypatch):
    import uuid

    import boto3

    endpoint = os.environ.get("INTARIS_SPOOL_TEST_S3")
    if not endpoint:
        pytest.skip("Disposable S3 HTTP service not configured")
    bucket = "spool-test-" + uuid.uuid4().hex
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="spool-test",
        aws_secret_access_key="spool-test",
    )
    client.create_bucket(Bucket=bucket)
    cfg = replace(
        config,
        backend="s3",
        s3_endpoint=endpoint,
        s3_bucket=bucket,
        s3_access_key="spool-test",
        s3_secret_key="spool-test",
        event_cache_backend="disabled",
    )
    store = EventStore(cfg)
    try:
        store.set_session_store(sessions)
        store.recover_spool()
        for i in range(3):
            store.append("tenant", "recording", [EVENT], idempotency_key=str(i))
        original = store._spool.backend.append_file

        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("PUT committed but response was lost")

        monkeypatch.setattr(store._spool.backend, "append_file", lost_response)
        with pytest.raises(SpoolUnavailable):
            store.flush_all()
        assert len(client.list_objects_v2(Bucket=bucket)["Contents"]) == 1
        monkeypatch.setattr(store._spool.backend, "append_file", original)
        store.flush_all()  # Conditional PUT collision must verify exact bytes.
        assert len(client.list_objects_v2(Bucket=bucket)["Contents"]) == 1
        assert [e["seq"] for e in store.read("tenant", "recording")] == [1, 2, 3]
        assert store.availability("tenant", "recording").history_gap is None
        assert store.buffered_event_count == 0
    finally:
        store.close()
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                client.delete_object(Bucket=bucket, Key=obj["Key"])
        client.delete_bucket(Bucket=bucket)


@pytest.mark.parametrize("fsync", [True, False])
@pytest.mark.parametrize(
    "boundary",
    [
        "prepare",
        "reserve",
        "assign",
        "upload",
        "receipt",
        "retire",
    ],
)
def test_process_exit_at_every_commit_boundary(
    config, sessions, tmp_path, boundary, fsync, monkeypatch
):
    config.spool_fsync = fsync
    monkeypatch.setenv("EVENT_STORE_SPOOL_FSYNC", str(fsync).lower())
    worker = Path(__file__).with_name("spool_crash_worker.py")
    result = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), boundary],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 73, result.stderr
    recovered = EventStore(config)
    try:
        recovered.set_session_store(sessions)
        recovered.recover_spool()
        events = recovered.read("tenant", "recording")
        assert [e["seq"] for e in events] == [1]
        assert events[0]["data"] == EVENT["data"]
        timestamp = events[0]["ts"]
        assert recovered.append(
            "tenant", "recording", [EVENT], idempotency_key="same"
        ) == [1]
        recovered.flush_all()
        assert recovered.read("tenant", "recording")[0]["ts"] == timestamp
        assert recovered.availability("tenant", "recording").history_gap is None
        row = sessions.get("recording", user_id="tenant")
        assert row["last_event_seq"] == 1
        assert row["state_revision"] == 2
        assert recovered.buffered_event_count == 0
    finally:
        recovered.close()


def test_fsync_defaults_and_environment(monkeypatch):
    monkeypatch.delenv("EVENT_STORE_SPOOL_FSYNC", raising=False)
    assert EventStoreConfig().spool_fsync is True
    monkeypatch.setenv("EVENT_STORE_SPOOL_FSYNC", "false")
    assert EventStoreConfig().spool_fsync is False


@pytest.mark.parametrize("enabled", [True, False])
def test_spool_file_and_directory_barriers_follow_option(config, monkeypatch, enabled):
    from unittest.mock import MagicMock

    from intaris.events.spool import EventSpool

    config.spool_fsync = enabled
    backend = MagicMock()
    synced = []
    original = os.fsync

    def track(descriptor):
        synced.append(os.fstat(descriptor).st_mode)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", track)
    spool = EventSpool(config, backend)
    try:
        record = spool.prepare("tenant", "recording", [EVENT], "client", "batch")
        spool.assign(record, [1])
        spool.delete(("tenant", "recording"), lambda key: None)
        import stat

        assert any(stat.S_ISREG(mode) for mode in synced) == enabled
        assert any(stat.S_ISDIR(mode) for mode in synced) == enabled
    finally:
        spool.close()


def test_enabling_fsync_syncs_surviving_unsynced_files(config, monkeypatch):
    from unittest.mock import MagicMock

    from intaris.events.spool import EventSpool

    config.spool_fsync = False
    spool = EventSpool(config, MagicMock())
    spool.prepare("tenant", "recording", [EVENT], "client", "batch")
    spool.close()
    original = os.fsync
    files = set()

    def track(descriptor):
        files.add(os.fstat(descriptor).st_ino)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", track)
    config.spool_fsync = True
    reopened = EventSpool(config, MagicMock())
    try:
        pending = Path(config.spool_path) / "batches/batch.json"
        assert pending.stat().st_ino in files
    finally:
        reopened.close()
