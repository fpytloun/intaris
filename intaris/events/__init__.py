"""Session event store for full-fidelity session recording.

Captures the complete timeline of agent sessions as append-only ndjson
event logs, enabling live tailing, playback, reconstruction, and
behavioral analysis.

Components:
- EventBackend: Protocol for storage backends (filesystem, S3)
- EventBuffer: In-memory write buffer with deterministic flush triggers
- EventStore: High-level store combining backend + buffer + EventBus
"""

from __future__ import annotations
