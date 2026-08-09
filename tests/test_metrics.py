"""Tests for dependency-free performance metrics."""

from __future__ import annotations

import asyncio
import socket
import urllib.request

import pytest
from starlette.applications import Starlette

from intaris.metrics import (
    Histogram,
    RuntimeMetrics,
    monitor_event_loop_delay,
    render_prometheus_metrics,
)
from intaris.server import _start_metrics_server


def test_histogram_reports_cumulative_buckets_and_summary() -> None:
    histogram = Histogram((1, 10, 100))
    for value in (0.5, 5, 20):
        histogram.observe(value)

    snapshot = histogram.snapshot()

    assert snapshot["count"] == 3
    assert snapshot["sum"] == 25.5
    assert snapshot["avg_ms"] == 8.5
    assert snapshot["max_ms"] == 20
    assert snapshot["last_ms"] == 20
    assert snapshot["buckets"] == {
        "le_1ms": 1,
        "le_10ms": 2,
        "le_100ms": 3,
    }


def test_runtime_metrics_group_requests_by_route_template_and_status() -> None:
    metrics = RuntimeMetrics()
    metrics.observe_request("GET", "/session/{session_id}", 200, 12)
    metrics.observe_request("GET", "/session/{session_id}", 200, 18)
    metrics.observe_request("GET", "/session/{session_id}", 404, 3)

    snapshot = metrics.snapshot()["http"]

    assert snapshot["GET /session/{session_id} 2xx"]["requests_total"] == 2
    assert snapshot["GET /session/{session_id} 2xx"]["latency"]["avg_ms"] == 15
    assert snapshot["GET /session/{session_id} 4xx"]["requests_total"] == 1


@pytest.mark.asyncio
async def test_event_loop_monitor_records_samples() -> None:
    metrics = RuntimeMetrics()
    task = asyncio.create_task(
        monitor_event_loop_delay(metrics, interval_seconds=0.001)
    )
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert metrics.snapshot()["event_loop_delay"]["count"] >= 1


def test_prometheus_renderer_exports_histograms_and_bounded_http_labels() -> None:
    metrics = RuntimeMetrics()
    metrics.observe_request("GET", "/session/{session_id}", 200, 12)

    rendered = render_prometheus_metrics(
        metrics.snapshot(),
        {
            "query_latency": Histogram().snapshot(),
            "transaction_latency": Histogram().snapshot(),
            "pool_wait_latency": Histogram().snapshot(),
        },
        {
            "buffered_events": 3,
            "backend": {
                "operation_latency": {
                    "get": Histogram((10, 100)).snapshot(),
                }
            },
        },
    )

    assert "intaris_up 1" in rendered
    assert (
        'intaris_http_requests_total{method="GET",'
        'route="/session/{session_id}",status_group="2xx"} 1'
    ) in rendered
    assert "intaris_http_request_duration_milliseconds_bucket{" in rendered
    assert "intaris_event_store_buffered_events 3" in rendered
    assert (
        "# TYPE intaris_event_store_backend_operation_latency_get histogram" in rendered
    )


@pytest.mark.asyncio
async def test_dedicated_metrics_listener_serves_without_auth(monkeypatch) -> None:
    from intaris import server as server_module

    class FakeDatabase:
        def metrics(self):
            empty = Histogram().snapshot()
            return {
                "query_latency": empty,
                "transaction_latency": empty,
                "pool_wait_latency": empty,
            }

    source_app = Starlette()
    source_app.state.runtime_metrics = RuntimeMetrics()
    source_app.state.event_store = None
    monkeypatch.setattr(server_module, "_db", FakeDatabase())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    server, task = await _start_metrics_server(
        source_app,
        host="127.0.0.1",
        port=port,
    )
    try:
        response = await asyncio.to_thread(
            urllib.request.urlopen,
            f"http://127.0.0.1:{port}/metrics",
        )
        body = response.read().decode()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)

    assert response.status == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "intaris_up 1" in body
