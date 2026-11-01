"""Codec compatibility, compressed archives and frozen export recovery."""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import replace

import pytest
import zstandard

from intaris.config import EventStoreConfig
from intaris.events.backend import (
    FilesystemEventBackend,
    S3EventBackend,
    _ndjson_to_events,
    _parse_chunk_filename,
)
from intaris.events.codec import decode, encode, iter_events, write_chunk
from intaris.events.spool import SpoolUnavailable, batch_identity, request_digest
from intaris.events.store import EventStore


@pytest.mark.parametrize(
    "value",
    [
        {"text": "Příliš žluťoučký kůň 🐈", "nested": [True, None, 1e-7]},
        {"large": 2**100},
        {"surrogate": "\ud800"},
        {1: "integer key", None: "null key"},
    ],
)
def test_payload_semantics_match_stdlib(value):
    assert decode(encode(value)) == json.loads(json.dumps(value))


def test_legacy_nonfinite_values_are_not_silently_changed():
    value = {"nan": float("nan"), "infinity": float("inf")}
    restored = decode(encode(value))
    assert math.isnan(restored["nan"])
    assert restored["infinity"] == float("inf")


def test_identity_encodings_are_unchanged():
    events = [{"type": "message", "data": {"text": "Žluťoučký 🐈", "float": 1e-7}}]
    assert (
        request_digest(events, "source")
        == hashlib.sha256(
            json.dumps(
                ["source", events], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    assert (
        batch_identity("tenant", "session", "Žluťoučký")
        == hashlib.sha256(
            json.dumps(["tenant", "session", "Žluťoučký"]).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("compression", ["none", "zstd"])
def test_chunk_roundtrip_and_truncated_compression(compression):
    events = [{"seq": i, "data": {"content": "sample " * 1000}} for i in range(20)]
    output = io.BytesIO()
    write_chunk(output, iter(events), compression=compression)
    payload = output.getvalue()
    assert list(iter_events(payload)) == events
    if compression == "zstd":
        assert len(payload) < len(json.dumps(events).encode())
        for damaged in (payload[:-1], payload[:-5], payload + b"trailing"):
            with pytest.raises((ValueError, zstandard.ZstdError)):
                list(iter_events(damaged))
        damaged = bytearray(payload)
        damaged[-1] ^= 1
        with pytest.raises(zstandard.ZstdError):
            list(iter_events(bytes(damaged)))


@pytest.mark.parametrize("read_compression", ["none", "zstd"])
def test_all_read_paths_support_mixed_archives(tmp_path, read_compression):
    config = EventStoreConfig(filesystem_path=str(tmp_path), compression="none")
    legacy = FilesystemEventBackend(config)
    compressed = FilesystemEventBackend(replace(config, compression="zstd"))
    rows = [
        {"seq": i, "type": "message", "source": "test", "ts": "t", "data": {}}
        for i in range(1, 5)
    ]
    legacy.append("tenant", "session", rows[:2])
    compressed.append("tenant", "session", rows[2:])
    reader = FilesystemEventBackend(replace(config, compression=read_compression))
    assert reader.read("tenant", "session") == rows
    assert reader.read_seqs("tenant", "session", {2, 3}) == rows[1:3]
    assert reader.read_before("tenant", "session", 4, 2) == rows[1:3]
    assert reader.read_tail("tenant", "session", 2) == rows[2:]
    assert list(reader.read_stream("tenant", "session", 1)) == rows[1:]
    assert reader.last_seq("tenant", "session") == 4
    assert reader.available_seq_ranges("tenant", "session") == [(1, 2), (3, 4)]
    assert _parse_chunk_filename("seq_000003_000004.ndjson.zst") == (3, 4)


def test_limited_read_validates_frame_before_exposing_records(tmp_path):
    backend = FilesystemEventBackend(EventStoreConfig(filesystem_path=str(tmp_path)))
    backend.append("tenant", "session", [{"seq": i} for i in range(1, 1501)])
    chunk = next(tmp_path.rglob("*.zst"))
    chunk.write_bytes(chunk.read_bytes()[:-1])
    with pytest.raises(ValueError, match="Incomplete compressed"):
        backend.read("tenant", "session", limit=1)
    with pytest.raises(ValueError, match="Incomplete compressed"):
        next(backend.read_stream("tenant", "session"))


@pytest.mark.parametrize("original_compression", ["none", "zstd"])
@pytest.mark.parametrize("sealed", [False, True])
def test_frozen_export_survives_compression_default_change(
    tmp_path, monkeypatch, original_compression, sealed
):
    config = EventStoreConfig(
        filesystem_path=str(tmp_path / "archive"),
        spool_path=str(tmp_path / "spool"),
        compression=original_compression,
        flush_size=100,
    )
    store = EventStore(config)
    event = {"type": "message", "data": {"text": "original payload 🐈"}}
    store.append("tenant", "session", [event])
    archive = store._spool.backend
    if sealed:
        # The sealed digest must survive a lost successful upload response.
        original = archive.append_file

        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Lost response")

        monkeypatch.setattr(archive, "append_file", lose_response)
    else:

        def fail_seal(*args, **kwargs):
            raise OSError("Sealing interrupted")

        monkeypatch.setattr("intaris.events.spool.write_chunk", fail_seal)
    try:
        with pytest.raises(SpoolUnavailable):
            store.flush_all()
    finally:
        store._spool.close()
        store._spool = None
    monkeypatch.undo()
    new_config = replace(
        config, compression="zstd" if original_compression == "none" else "none"
    )
    restarted = EventStore(new_config)
    try:
        restarted.recover_spool()
        restarted.flush_all()
        chunks = list((tmp_path / "archive").rglob("*.ndjson*"))
        assert len(chunks) == 1
        assert chunks[0].name.endswith(
            ".ndjson.zst" if original_compression == "zstd" else ".ndjson"
        )
        restored = _ndjson_to_events(chunks[0].read_bytes())
        assert restored[0]["data"] == event["data"]
        assert restored[0]["seq"] == 1
    finally:
        restarted.close()


@pytest.mark.parametrize("spool_enabled", [False, True])
def test_s3_compressed_upload_and_cached_replay(tmp_path, monkeypatch, spool_enabled):
    import boto3
    from botocore.exceptions import ClientError

    class ObjectStore:
        def __init__(self):
            self.objects = {}
            self.gets = 0

        def head_bucket(self, **kwargs):
            return {}

        def list_objects_v2(self, *, Prefix, **kwargs):
            return {
                "Contents": [
                    {"Key": key, "ETag": hashlib.sha256(data).hexdigest()}
                    for key, data in self.objects.items()
                    if key.startswith(Prefix)
                ]
            }

        def put_object(self, *, Key, Body, ContentType, IfNoneMatch, **kwargs):
            assert IfNoneMatch == "*"
            assert ContentType == "application/zstd"
            if Key in self.objects:
                raise ClientError(
                    {"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject"
                )
            data = Body if isinstance(Body, bytes) else Body.read()
            self.objects[Key] = data
            return {"ETag": hashlib.sha256(data).hexdigest()}

        def get_object(self, *, Key, **kwargs):
            self.gets += 1
            return {"Body": io.BytesIO(self.objects[Key])}

    objects = ObjectStore()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: objects)
    config = EventStoreConfig(
        backend="s3",
        spool_enabled=spool_enabled,
        spool_path=str(tmp_path / "spool"),
        event_cache_path=str(tmp_path / "cache"),
    )
    assert config.compression == "zstd"
    store = EventStore(config)
    try:
        store.append(
            "tenant", "session", [{"type": "message", "data": {"text": "hello 🐈"}}]
        )
        store.flush_all()
        assert len(objects.objects) == 1
        key, payload = next(iter(objects.objects.items()))
        assert key.endswith(".ndjson.zst")
        reader = S3EventBackend(config)
        first = reader.read("tenant", "session")
        assert first[0]["data"] == {"text": "hello 🐈"}
        assert reader.read_tail("tenant", "session", 1) == first
        assert list(reader.read_stream("tenant", "session")) == first
        # Byte writes populate the cache; streaming file writes load it lazily.
        assert objects.gets == int(spool_enabled)
        writer = store.metrics()["backend"]["chunks"]
        assert writer["events"]["count"] == 1
        assert writer["events"]["sum"] == 1
        assert writer["stored_bytes"]["sum"] == len(payload)
        reader.append("tenant", "session", first)
        assert objects.gets == int(spool_enabled) + 1
        confirmed = reader.metrics()["chunks"]
        assert confirmed["events"]["count"] == 1
        assert confirmed["stored_bytes"]["sum"] == len(payload)

        def unavailable(**kwargs):
            raise OSError("S3 temporarily unavailable")

        monkeypatch.setattr(objects, "put_object", unavailable)
        with pytest.raises(OSError, match="temporarily unavailable"):
            reader.append("tenant", "session", [{**first[0], "seq": 2}])
        assert reader.metrics()["chunks"] == confirmed
    finally:
        store.close()
