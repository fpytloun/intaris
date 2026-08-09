"""Small dependency-free performance metrics for health diagnostics."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import defaultdict
from typing import Any

DEFAULT_LATENCY_BUCKETS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)


class Histogram:
    """Thread-safe cumulative histogram with bounded fixed buckets."""

    def __init__(self, buckets_ms: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS_MS):
        self._buckets = tuple(sorted(buckets_ms))
        self._counts = [0] * len(self._buckets)
        self._count = 0
        self._sum = 0.0
        self._max = 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def observe(self, value_ms: float) -> None:
        value = max(0.0, float(value_ms))
        with self._lock:
            self._count += 1
            self._sum += value
            self._max = max(self._max, value)
            self._last = value
            for index, boundary in enumerate(self._buckets):
                if value <= boundary:
                    self._counts[index] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            count = self._count
            return {
                "count": count,
                "sum": round(self._sum, 3),
                "avg_ms": round(self._sum / count, 3) if count else 0.0,
                "max_ms": round(self._max, 3),
                "last_ms": round(self._last, 3),
                "buckets": {
                    f"le_{boundary:g}ms": bucket_count
                    for boundary, bucket_count in zip(
                        self._buckets, self._counts, strict=True
                    )
                },
            }


class RuntimeMetrics:
    """Process-local HTTP and event-loop performance metrics."""

    def __init__(self) -> None:
        self._request_lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], Histogram] = {}
        self._request_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self.event_loop_delay = Histogram((1, 5, 10, 25, 50, 100, 250, 500))

    def observe_request(
        self,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        status_group = f"{status_code // 100}xx"
        key = (method, route, status_group)
        with self._request_lock:
            histogram = self._requests.get(key)
            if histogram is None:
                histogram = Histogram()
                self._requests[key] = histogram
            self._request_counts[key] += 1
        histogram.observe(duration_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._request_lock:
            items = list(self._requests.items())
            counts = dict(self._request_counts)
        return {
            "http": {
                f"{method} {route} {status_group}": {
                    "requests_total": counts[(method, route, status_group)],
                    "latency": histogram.snapshot(),
                }
                for (method, route, status_group), histogram in items
            },
            "event_loop_delay": self.event_loop_delay.snapshot(),
        }


def render_prometheus_metrics(
    runtime: dict[str, Any],
    database: dict[str, Any],
    event_store: dict[str, Any] | None,
) -> str:
    """Render process-local metrics in Prometheus text exposition format."""
    lines = [
        "# HELP intaris_up Whether the Intaris process can render metrics.",
        "# TYPE intaris_up gauge",
        "intaris_up 1",
        "# TYPE intaris_http_requests_total counter",
        "# TYPE intaris_http_request_duration_milliseconds histogram",
        "# TYPE intaris_event_loop_delay_milliseconds histogram",
        "# TYPE intaris_database_query_latency_milliseconds histogram",
        "# TYPE intaris_database_transaction_latency_milliseconds histogram",
        "# TYPE intaris_database_pool_wait_latency_milliseconds histogram",
    ]
    for key, values in sorted(runtime.get("http", {}).items()):
        method, route, status_group = key.split(" ", 2)
        labels = {
            "method": method,
            "route": route,
            "status_group": status_group,
        }
        lines.append(
            _sample(
                "intaris_http_requests_total",
                values.get("requests_total", 0),
                labels,
            )
        )
        _render_histogram(
            lines,
            "intaris_http_request_duration_milliseconds",
            values.get("latency", {}),
            labels,
        )
    _render_histogram(
        lines,
        "intaris_event_loop_delay_milliseconds",
        runtime.get("event_loop_delay", {}),
    )
    for name in ("query_latency", "transaction_latency", "pool_wait_latency"):
        _render_histogram(
            lines,
            f"intaris_database_{name}_milliseconds",
            database.get(name, {}),
        )
    if event_store is not None:
        _render_numeric_tree(lines, "intaris_event_store", event_store)
    return "\n".join(lines) + "\n"


def _render_numeric_tree(lines: list[str], prefix: str, value: Any) -> None:
    if isinstance(value, dict):
        if {"count", "sum", "buckets"}.issubset(value):
            lines.append(f"# TYPE {prefix} histogram")
            _render_histogram(lines, prefix, value)
            return
        for key, child in sorted(value.items()):
            _render_numeric_tree(lines, f"{prefix}_{_sanitize_metric_name(key)}", child)
        return
    if isinstance(value, bool):
        lines.append(f"{prefix} {int(value)}")
    elif isinstance(value, int | float):
        lines.append(f"{prefix} {value}")


def _render_histogram(
    lines: list[str],
    name: str,
    histogram: dict[str, Any],
    labels: dict[str, str] | None = None,
) -> None:
    if not histogram:
        return
    for bucket, count in histogram.get("buckets", {}).items():
        boundary = bucket.removeprefix("le_").removesuffix("ms")
        bucket_labels = dict(labels or {})
        bucket_labels["le"] = boundary
        lines.append(_sample(f"{name}_bucket", count, bucket_labels))
    infinite_labels = dict(labels or {})
    infinite_labels["le"] = "+Inf"
    lines.append(_sample(f"{name}_bucket", histogram.get("count", 0), infinite_labels))
    lines.append(_sample(f"{name}_sum", histogram.get("sum", 0), labels))
    lines.append(_sample(f"{name}_count", histogram.get("count", 0), labels))


def _sample(
    name: str,
    value: int | float,
    labels: dict[str, str] | None = None,
) -> str:
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(
        f'{key}="{_escape_label(label)}"' for key, label in sorted(labels.items())
    )
    return f"{name}{{{rendered}}} {value}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _sanitize_metric_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_:]", "_", value)


async def monitor_event_loop_delay(
    metrics: RuntimeMetrics,
    *,
    interval_seconds: float = 1.0,
) -> None:
    """Measure scheduler delay until cancellation."""
    deadline = time.monotonic() + interval_seconds
    while True:
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        now = time.monotonic()
        metrics.event_loop_delay.observe(max(0.0, now - deadline) * 1000)
        deadline = now + interval_seconds
