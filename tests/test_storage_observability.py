"""Content-free metrics through real storage, streaming and scrape boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest

from intaris.config import EventStoreConfig
from intaris.events import observability
from intaris.events.store import EventStore
from intaris.metrics import Histogram, render_prometheus_metrics


@pytest.fixture(params=[False, True], ids=["memory", "spool"])
def store(tmp_path, request):
    config = EventStoreConfig(
        filesystem_path=str(tmp_path / "archive"),
        spool_path=str(tmp_path / "spool"),
        spool_enabled=request.param,
        flush_size=2000,
    )
    storage = EventStore(config)
    try:
        yield storage
    finally:
        storage.close()


def rows(count):
    return [{"type": "message", "data": {"text": "private-recording-content"}}] * count


def test_append_outcomes_and_pending_metrics(store, monkeypatch):
    store.append("private-tenant", "private-session", rows(3))
    store.append("private-tenant", "private-session", [])
    with pytest.raises(ValueError):
        store.append("..", "private-session", rows(1))
    snapshot = store.metrics()
    assert snapshot["append_requests_total"] == 3
    assert snapshot["append_failures_total"] == 1
    assert snapshot["append_latency_milliseconds"]["count"] == 3
    assert snapshot["buffered_sessions"] == 1
    assert snapshot["buffered_events"] == 3
    assert snapshot["pending_payload_bytes"] > 0
    assert snapshot["oldest_pending_age_seconds"] >= 0
    archive = store._spool.backend if store._spool else store._backend
    with monkeypatch.context() as patch:
        patch.setattr(
            archive, "read", lambda *a, **k: pytest.fail("Metrics read archive")
        )
        patch.setattr(archive, "_list_chunks", lambda *a: pytest.fail("Metrics scan"))
        if store._spool:
            patch.setattr(
                store._spool, "sessions", lambda: pytest.fail("Metrics read manifests")
            )
        rendered = render_prometheus_metrics({}, {}, store.metrics())
    for value in ("private-tenant", "private-session", "private-recording-content"):
        assert value not in rendered
        assert value not in json.dumps(snapshot)
    assert "intaris_event_store_pending_payload_bytes " in rendered
    assert "intaris_event_store_append_latency_milliseconds_bucket" in rendered
    store.flush_all()
    drained = store.metrics()
    assert drained["pending_payload_bytes"] == 0
    assert drained["oldest_pending_age_seconds"] == 0


def test_persisted_backlog_age_survives_restart(tmp_path, monkeypatch):
    config = EventStoreConfig(
        filesystem_path=str(tmp_path / "archive"),
        spool_path=str(tmp_path / "spool"),
    )
    store = EventStore(config)
    store.append("tenant", "session", rows(2))
    timestamp = next(iter(store._spool._index.values()))["ts"]
    store.close()
    restarted = EventStore(config)
    try:
        from intaris.events import spool

        clock = datetime.fromisoformat(timestamp).timestamp()
        monkeypatch.setattr(spool.time, "time", lambda: clock + 60)
        assert restarted.metrics()["oldest_pending_age_seconds"] == 60
        assert restarted.metrics()["spool"]["pending_events"] == 2
        monkeypatch.setattr(spool.time, "time", lambda: clock - 60)
        assert restarted.metrics()["oldest_pending_age_seconds"] == 0
    finally:
        restarted.close()


def test_filtered_decode_amplification_and_empty_results(store):
    events = rows(250)
    events[-1] = {"type": "tool_result", "data": {"result": "done"}}
    store.append("tenant", "session", events)
    store.flush_all()
    assert (
        len(store.read("tenant", "session", event_types={"tool_result"}, limit=1)) == 1
    )
    forward = store.metrics()["reads"]["forward"]
    assert forward["requests_total"] == 1
    assert forward["returned_events_total"] == 1
    assert forward["decoded_events_total"] == 250
    assert forward["chunk_reads_total"] == 1
    assert forward["chunk_bytes_total"] > 0
    assert forward["decode_amplification"]["sum"] == 250
    assert store.read("tenant", "session", event_types={"evaluation"}, limit=1) == []
    forward = store.metrics()["reads"]["forward"]
    assert forward["requests_total"] == 2
    assert forward["zero_result_requests_total"] == 1
    assert forward["decode_amplification"]["count"] == 1


def test_nested_tail_read_is_counted_once(store):
    store.append("tenant", "session", rows(20))
    store.flush_all()
    assert len(store.read_tail("tenant", "session", 2)) == 2
    reads = store.metrics()["reads"]
    assert reads["tail"]["requests_total"] == 1
    assert reads["tail"]["returned_events_total"] == 2
    assert reads["before"]["requests_total"] == 0
    assert reads["forward"]["requests_total"] == 0


def test_stream_context_is_reset_between_yields_and_on_close(store, monkeypatch):
    store.append("tenant", "session", rows(10))
    store.flush_all()
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(
        observability, "time", SimpleNamespace(monotonic=lambda: clock.value)
    )
    first = store.read_stream("tenant", "session")
    second = store.read_stream("tenant", "session", after_seq=5)
    assert next(first)["seq"] == 1
    clock.value += 300  # Downstream time is not storage latency.
    assert next(second)["seq"] == 6
    store.read_seqs("tenant", "session", {10})
    first.close()
    assert list(second)[-1]["seq"] == 10
    reads = store.metrics()["reads"]
    assert reads["exact"]["requests_total"] == 1
    assert reads["stream"]["requests_total"] == 2
    assert reads["stream"]["abandoned_total"] == 1
    assert reads["stream"]["failures_total"] == 0
    assert reads["stream"]["returned_events_total"] == 6
    assert reads["stream"]["latency_milliseconds"]["sum"] == 0
    assert reads["stream"]["decoded_events_total"] == 11
    assert observability._active_read.get() is None


def test_decode_failure_is_counted_without_changing_integrity_behavior(store):
    store.append("tenant", "session", rows(10))
    store.flush_all()
    archive = store._spool.backend if store._spool else store._backend
    chunk = next(archive._base_path.rglob("*.zst"))
    chunk.write_bytes(chunk.read_bytes()[:-1])
    with pytest.raises(ValueError, match="Incomplete compressed"):
        list(store.read_stream("tenant", "session"))
    stream = store.metrics()["reads"]["stream"]
    assert stream["requests_total"] == stream["failures_total"] == 1
    assert stream["decoded_events_total"] == stream["returned_events_total"] == 0
    assert observability._active_read.get() is None


def test_concurrent_exact_reads_are_isolated(store):
    store.append("tenant", "session", rows(20))
    store.flush_all()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: store.read_seqs("tenant", "session", {10}),
                range(12),
            )
        )
    assert all(len(result) == 1 for result in results)
    exact = store.metrics()["reads"]["exact"]
    assert exact["requests_total"] == exact["returned_events_total"] == 12
    assert exact["decoded_events_total"] == 20 * 12


def test_chunk_distributions_measure_each_chunk_not_the_flush_sum(store):
    if not store._spool:
        pytest.skip("Multiple chunks per flush is the durable spool export path")
    # Keep queued batches separate, then export them in three chunks.
    for _ in range(3):
        store.append("tenant", "session", rows(2))
    store._config.flush_size = 2
    store.flush_all()
    chunks = store.metrics()["backend"]["chunks"]
    archive_files = list(store._spool.backend._base_path.rglob("*.zst"))
    assert chunks["events"]["count"] == 3
    assert chunks["events"]["sum"] == 6
    assert chunks["stored_bytes"]["count"] == 3
    assert chunks["stored_bytes"]["sum"] == sum(p.stat().st_size for p in archive_files)


def test_unitless_histograms_preserve_prometheus_units():
    histogram = Histogram((1, 10, 100), unit="")
    histogram.observe(4)
    snapshot = histogram.snapshot()
    assert snapshot["avg"] == 4
    assert "avg_ms" not in snapshot
    assert snapshot["buckets"] == {"le_1": 0, "le_10": 1, "le_100": 1}
    rendered = render_prometheus_metrics({}, {}, {"chunk_stored_bytes": snapshot})
    assert 'intaris_event_store_chunk_stored_bytes_bucket{le="10"} 1' in rendered


def test_failed_memory_flush_preserves_oldest_pending_age(tmp_path, monkeypatch):
    import time

    storage = EventStore(
        EventStoreConfig(filesystem_path=str(tmp_path / "archive"), spool_enabled=False)
    )
    try:
        storage.append("tenant", "session", rows(2))
        storage._buffer_started_at[("tenant", "session")] = time.monotonic() - 60

        def unavailable(*args, **kwargs):
            raise OSError("Archive temporarily unavailable")

        monkeypatch.setattr(storage._backend, "append", unavailable)
        storage.flush_all()
        snapshot = storage.metrics()
        assert snapshot["flush_failures_total"] == 1
        assert snapshot["oldest_pending_age_seconds"] >= 60
        assert snapshot["buffered_events"] == 2
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_actual_metrics_endpoint_exports_storage_observations(store, monkeypatch):
    import httpx
    from starlette.applications import Starlette

    from intaris import server
    from intaris.metrics import RuntimeMetrics

    store.append("private-tenant", "private-session", rows(3))
    store.flush_all()
    store.read("private-tenant", "private-session", limit=1)
    source = Starlette()
    source.state.runtime_metrics = RuntimeMetrics()
    source.state.event_store = store
    monkeypatch.setattr(server, "_db", SimpleNamespace(metrics=lambda: {}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.create_metrics_app(source)),
        base_url="http://metrics.test",
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "intaris_event_store_reads_forward_returned_events_total 1" in response.text
    assert "intaris_event_store_backend_chunks_events_count 1" in response.text
    assert "private-" not in response.text


@pytest.mark.parametrize("cancel_from", ["producer", "consumer"])
def test_cancelled_stream_propagates_and_finalizes_once(
    store, monkeypatch, cancel_from
):
    import asyncio

    store.append("tenant", "session", rows(3))
    store.flush_all()
    original = store._backend.read_stream
    closed = []

    def cancelled(*args, **kwargs):
        underlying = original(*args, **kwargs)
        try:
            yield next(underlying)
            raise asyncio.CancelledError()
        finally:
            underlying.close()
            closed.append(True)

    monkeypatch.setattr(store._backend, "read_stream", cancelled)
    iterator = store.read_stream("tenant", "session")
    assert next(iterator)["seq"] == 1
    assert observability._active_read.get() is None
    with pytest.raises(asyncio.CancelledError):
        if cancel_from == "producer":
            next(iterator)
        else:
            iterator.throw(asyncio.CancelledError())
    iterator.close()
    assert closed == [True]
    snapshot = store.metrics()["reads"]["stream"]
    assert snapshot["requests_total"] == 1
    assert snapshot["failures_total"] == 1
    assert snapshot["returned_events_total"] == 1
    assert observability._active_read.get() is None
    assert store.read_seqs("tenant", "session", [2])[0]["seq"] == 2
    assert store.metrics()["reads"]["exact"]["requests_total"] == 1
    assert store.metrics()["reads"]["stream"] == snapshot
