"""Identity-only analytics progress, shared by L2 selection and L3 freshness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intaris.analytics_evidence import metadata


def source_revision(record: dict[str, Any]) -> list[Any]:
    """The persisted resolution identity, independent of proposal payloads."""
    return [
        record.get(k)
        for k in ("resolved_at", "resolved_by", "user_decision", "resolution_scope")
    ]


@dataclass
class SourceProgress:
    """Retain source identities, never historical audit or event payloads."""

    calls: dict[str, list[Any]] = field(default_factory=dict)
    reasoning_ids: set[str] = field(default_factory=set)
    event_seqs: set[int] = field(default_factory=set)
    legacy_cutoff: str = ""

    @classmethod
    def load(cls, db: Any, user_id: str, session_id: str) -> SourceProgress:
        progress = cls()
        from intaris.session_modes import analytics_scope

        scope = analytics_scope(db, user_id)
        cursor = None
        while True:
            params: list[Any] = [user_id, session_id]
            after = ""
            if cursor is not None:
                after = " AND (created_at > ? OR (created_at = ? AND id > ?))"
                params.extend([cursor[0], cursor[0], cursor[1]])
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, created_at, evidence_metadata, window_end "
                    "FROM session_summaries WHERE user_id = ? AND session_id = ? "
                    "AND summary_type = 'window'"
                    + after
                    + " ORDER BY created_at, id LIMIT 64",
                    params,
                )
                rows = cur.fetchall()
            for row in rows:
                if not scope.usable_summary({**dict(row), "session_id": session_id}):
                    continue
                evidence = metadata(row["evidence_metadata"])
                if not evidence:
                    progress.legacy_cutoff = max(
                        progress.legacy_cutoff, str(row["window_end"])
                    )
                progress.calls.update(evidence.get("calls", {}))
                progress.reasoning_ids.update(evidence.get("reasoning_ids", []))
                progress.event_seqs.update(evidence.get("event_seqs", []))
            if len(rows) < 64:
                return progress
            cursor = rows[-1]["created_at"], rows[-1]["id"]

    def call_status(self, record: dict[str, Any]) -> tuple[bool, bool]:
        """Return (uncovered, resolution_update), preserving legacy semantics."""
        key = record["call_id"]
        if key in self.calls:
            changed = self.calls[key] != source_revision(record)
            return changed, changed
        if str(record.get("timestamp", "")) > self.legacy_cutoff:
            return True, False
        changed = str(record.get("resolved_at") or "") > self.legacy_cutoff
        return changed, changed

    def reasoning_uncovered(self, record: dict[str, Any]) -> bool:
        return (
            record["call_id"] not in self.reasoning_ids
            and str(record.get("timestamp", "")) > self.legacy_cutoff
        )

    def event_uncovered(self, event: dict[str, Any]) -> bool:
        return (
            event.get("seq") not in self.event_seqs
            and str(event.get("ts", "")) > self.legacy_cutoff
        )
