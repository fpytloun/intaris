"""Byte-oriented event JSON and self-identifying archive compression.

Identity digests deliberately remain on their established stdlib JSON encoding.
These helpers only encode payloads whose integrity is checked on stored bytes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from typing import Any, BinaryIO

import msgspec
import zstandard

_ENCODER = msgspec.json.Encoder()
_DECODER = msgspec.json.Decoder()
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _json_value(value: Any) -> bool:
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _json_value(item) for key, item in value.items()
        )
    return False


def encode(value: Any) -> bytes:
    """Encode JSON payloads without changing legacy nonstandard value semantics."""
    if _json_value(value):
        try:
            return _ENCODER.encode(value)
        except (TypeError, UnicodeEncodeError):
            pass
    return json.dumps(value, separators=(",", ":")).encode()


def decode(data: bytes | bytearray) -> Any:
    """Decode UTF-8 bytes, including legacy NaN and escaped surrogate values."""
    try:
        return _DECODER.decode(data)
    except msgspec.DecodeError:
        return json.loads(data)


def write_chunk(output: BinaryIO, events: Iterable[dict], *, compression: str) -> None:
    """Write one sealed chunk; close the compression frame before syncing."""
    if compression not in ("none", "zstd"):
        raise ValueError(f"Unsupported event compression: {compression}")
    writer = (
        zstandard.ZstdCompressor(level=1, write_checksum=True).stream_writer(
            output, closefd=False
        )
        if compression == "zstd"
        else nullcontext(output)
    )
    with writer as stream:
        for event in events:
            stream.write(encode(event) + b"\n")


def _parts(data: bytes) -> Iterator[bytes]:
    if not data.startswith(_ZSTD_MAGIC):
        yield data
        return
    # Consume small compressed pieces rather than materializing the entire
    # expanded chunk. Check EOF explicitly: a truncated frame is not valid NDJSON.
    decoder = zstandard.ZstdDecompressor().decompressobj()
    for offset in range(0, len(data), 4096):
        part = data[offset : offset + 4096]
        yield decoder.decompress(part)
        if decoder.eof:
            if decoder.unused_data or offset + len(part) != len(data):
                raise ValueError("Trailing data after compressed event chunk")
            return
    raise ValueError("Incomplete compressed event chunk")


def iter_events(data: bytes) -> Iterator[dict]:
    """Decode legacy NDJSON or one checksummed Zstandard frame incrementally."""
    if data.startswith(_ZSTD_MAGIC):
        # A limited read or a projection batch can stop before the trailer.
        # Validate the entire frame before exposing records. A second bounded
        # decompression pass avoids retaining the expanded chunk in memory;
        # JSON is still decoded only once.
        for _ in _parts(data):
            pass
    pending = bytearray()
    for part in _parts(data):
        pending.extend(part)
        start = 0
        while (end := pending.find(b"\n", start)) >= 0:
            line = pending[start:end].strip()
            if line:
                yield decode(line)
            start = end + 1
        del pending[:start]
    if pending.strip():
        yield decode(pending)
