"""Bounded, content-free measurements at the public session-read boundary."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from intaris.events.codec import iter_events
from intaris.metrics import Histogram


@dataclass
class _Read:
    decoded: int = 0
    returned: int = 0
    chunks: int = 0
    stored_bytes: int = 0
    duration_ms: float = 0
    failed: bool = False
    abandoned: bool = False


_active_read: ContextVar[_Read | None] = ContextVar("event_read", default=None)


def iter_observed_events(data: bytes) -> Iterator[dict]:
    """Measure archive decode work without serializing or inspecting content."""
    observation = _active_read.get()
    if observation is not None:
        observation.chunks += 1
        observation.stored_bytes += len(data)
    for event in iter_events(data):
        if observation is not None:
            observation.decoded += 1
        yield event


class ReadMetrics:
    """Fixed operation registry; nested public reads belong to their outer read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, dict[str, Any]] = {}
        for operation in ("forward", "exact", "before", "tail", "stream"):
            self._operations[operation] = {
                **dict.fromkeys(
                    (
                        "requests_total",
                        "failures_total",
                        "abandoned_total",
                        "zero_result_requests_total",
                        "returned_events_total",
                        "decoded_events_total",
                        "chunk_reads_total",
                        "chunk_bytes_total",
                    ),
                    0,
                ),
                "latency_milliseconds": Histogram(),
                "decode_amplification": Histogram(
                    (0, 1, 2, 5, 10, 25, 50, 100, 250, 1000), unit=""
                ),
            }

    def record(self, operation: str, observation: _Read) -> None:
        """Record one finished, failed or explicitly closed read."""
        with self._lock:
            metrics = self._operations[operation]
            metrics["requests_total"] += 1
            metrics["failures_total"] += observation.failed
            metrics["abandoned_total"] += observation.abandoned
            metrics["zero_result_requests_total"] += (
                observation.returned == 0
                and not observation.failed
                and not observation.abandoned
            )
            metrics["returned_events_total"] += observation.returned
            metrics["decoded_events_total"] += observation.decoded
            metrics["chunk_reads_total"] += observation.chunks
            metrics["chunk_bytes_total"] += observation.stored_bytes
            metrics["latency_milliseconds"].observe(observation.duration_ms)
            if observation.returned:
                metrics["decode_amplification"].observe(
                    observation.decoded / observation.returned
                )

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot with no tenant, session or payload-derived labels."""
        with self._lock:
            return {
                operation: {
                    name: value.snapshot() if isinstance(value, Histogram) else value
                    for name, value in metrics.items()
                }
                for operation, metrics in self._operations.items()
            }


def observe_read(
    operation: str,
) -> Callable[[Callable[..., list[dict]]], Callable[..., list[dict]]]:
    """Instrument a list-returning read without counting nested public calls."""

    def decorate(
        function: Callable[..., list[dict]],
    ) -> Callable[..., list[dict]]:
        @wraps(function)
        def measured(self: Any, *args: Any, **kwargs: Any) -> list[dict]:
            if _active_read.get() is not None:
                return function(self, *args, **kwargs)
            observation = _Read()
            token = _active_read.set(observation)
            started = time.monotonic()
            try:
                result = function(self, *args, **kwargs)
                observation.returned = len(result)
                return result
            except BaseException:
                observation.failed = True
                raise
            finally:
                observation.duration_ms = (time.monotonic() - started) * 1000
                _active_read.reset(token)
                self._read_metrics.record(operation, observation)

        return measured

    return decorate


def observe_stream(
    function: Callable[..., Iterator[dict]],
) -> Callable[..., Iterator[dict]]:
    """Measure active iterator work, resetting context before every yield."""

    @wraps(function)
    def measured(self: Any, *args: Any, **kwargs: Any) -> Iterator[dict]:
        if _active_read.get() is not None:
            yield from function(self, *args, **kwargs)
            return
        observation = _Read()
        iterator = iter(function(self, *args, **kwargs))
        try:
            while True:
                token = _active_read.set(observation)
                started = time.monotonic()
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                finally:
                    observation.duration_ms += (time.monotonic() - started) * 1000
                    _active_read.reset(token)
                observation.returned += 1
                yield event
        except GeneratorExit:
            observation.abandoned = True
            raise
        except BaseException:
            observation.failed = True
            raise
        finally:
            try:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            except BaseException:
                observation.failed = True
                raise
            finally:
                self._read_metrics.record("stream", observation)

    return measured


class ChunkMetrics:
    """Actual confirmed chunk sizes, not aggregate sizes of flush invocations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events = Histogram((1, 10, 25, 50, 100, 250, 500, 1000), unit="")
        self._bytes = Histogram(
            (1024, 4096, 16384, 65536, 262144, 1048576, 4194304), unit=""
        )

    def observe(self, events: int, stored_bytes: int) -> None:
        """Include confirmed immutable-write retries, not just new objects."""
        with self._lock:
            self._events.observe(events)
            self._bytes.observe(stored_bytes)

    def snapshot(self) -> dict[str, Any]:
        """Return stored-byte and event-count distributions."""
        with self._lock:
            return {
                "events": self._events.snapshot(),
                "stored_bytes": self._bytes.snapshot(),
            }
