"""Upgrade tests for durable session state reconstructed from event logs."""

from __future__ import annotations

import sqlite3

from intaris.background import TaskQueue
from intaris.config import DBConfig, EventStoreConfig
from intaris.db import _SCHEMA_SQL_SQLITE, Database
from intaris.events.resolve import resolve_last_user_message
from intaris.events.store import EventStore
from intaris.session import SessionStore


def test_upgrade_reconciles_historical_user_message_state(tmp_path):
    """Startup reconciliation upgrades event-only sessions idempotently."""
    db_path = tmp_path / "prechange.db"
    prechange_schema = (
        _SCHEMA_SQL_SQLITE.replace("    last_event_seq INTEGER DEFAULT 0,\n", "")
        .replace("    latest_user_message_seq INTEGER,\n", "")
        .replace("    user_message_observed INTEGER DEFAULT 0,\n", "")
    )
    conn = sqlite3.connect(db_path)
    conn.executescript(prechange_schema)
    for session_id in ("sess-user-events", "sess-no-user"):
        conn.execute(
            """
            INSERT INTO sessions
                (user_id, session_id, intention, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "alice",
                session_id,
                "Initial intention",
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()

    event_config = EventStoreConfig(
        enabled=True,
        backend="filesystem",
        filesystem_path=str(tmp_path / "events"),
        flush_size=5,
        flush_interval=30,
    )
    historical_store = EventStore(event_config)
    assert historical_store.append(
        "alice",
        "sess-user-events",
        [
            {"type": "user_message", "data": {"content": "canonical user"}},
            {
                "type": "assistant_message",
                "data": {"content": "assistant response"},
            },
            {
                "type": "message",
                "data": {"role": "user", "text": "legacy user"},
            },
            {"type": "tool_call", "data": {"tool": "read"}},
        ],
        source="legacy-service",
    ) == [1, 2, 3, 4]
    assert historical_store.append(
        "alice",
        "sess-no-user",
        [
            {
                "type": "message",
                "data": {"role": "assistant", "text": "legacy assistant"},
            },
            {"type": "tool_call", "data": {"tool": "read"}},
        ],
        source="legacy-service",
    ) == [1, 2]
    historical_store.flush_all()

    upgraded_db = Database(DBConfig(path=str(db_path)))
    sessions = SessionStore(upgraded_db)
    startup_store = EventStore(event_config)
    startup_store.set_session_store(sessions)

    assert startup_store.reconcile_session_user_message_state() == 2
    user_session = sessions.get("sess-user-events", user_id="alice")
    assert user_session["last_event_seq"] == 4
    assert user_session["latest_user_message_seq"] == 3
    assert bool(user_session["user_message_observed"]) is True
    no_user_session = sessions.get("sess-no-user", user_id="alice")
    assert no_user_session["last_event_seq"] == 2
    assert no_user_session["latest_user_message_seq"] is None
    assert bool(no_user_session["user_message_observed"]) is False

    resolved = resolve_last_user_message(startup_store, "alice", "sess-user-events")
    assert resolved is not None
    assert resolved.content == "legacy user"
    assert resolved.seq == 3
    assert (
        TaskQueue(upgraded_db).enqueue_bootstrap_if_no_user_message(
            "alice", "sess-user-events"
        )
        is None
    )
    assert TaskQueue(upgraded_db).enqueue_bootstrap_if_no_user_message(
        "alice", "sess-no-user"
    )

    with upgraded_db.cursor() as cur:
        cur.execute(
            """
            UPDATE sessions
            SET last_event_seq = 10, latest_user_message_seq = 9
            WHERE user_id = 'alice' AND session_id = 'sess-user-events'
            """
        )

    restarted_db = Database(DBConfig(path=str(db_path)))
    restarted_sessions = SessionStore(restarted_db)
    restarted_store = EventStore(event_config)
    restarted_store.set_session_store(restarted_sessions)
    assert restarted_store.reconcile_session_user_message_state() == 2
    assert restarted_store.reconcile_session_user_message_state() == 2

    reconciled_again = restarted_sessions.get("sess-user-events", user_id="alice")
    assert reconciled_again["last_event_seq"] == 10
    assert reconciled_again["latest_user_message_seq"] == 9
    assert bool(reconciled_again["user_message_observed"]) is True
    no_user_again = restarted_sessions.get("sess-no-user", user_id="alice")
    assert no_user_again["last_event_seq"] == 2
    assert no_user_again["latest_user_message_seq"] is None
    assert bool(no_user_again["user_message_observed"]) is False
