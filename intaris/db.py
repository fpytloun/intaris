"""Database connection management for intaris.

Provides SQLite connection management with WAL mode for concurrent
read/write access. Table creation and indexes are handled here;
business logic lives in session.py and audit.py.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

from intaris.config import DBConfig

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager with WAL mode and thread-safe connections.

    Each thread gets its own connection via thread-local storage.
    WAL mode allows concurrent reads during writes.
    """

    def __init__(self, config: DBConfig):
        self._path = config.path
        self._local = threading.local()
        self._ensure_directory()
        self._ensure_tables()

    def _ensure_directory(self) -> None:
        """Create the database directory if it doesn't exist."""
        db_dir = os.path.dirname(self._path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local database connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for database operations.

        Commits on success, rolls back on exception.
        """
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for cursor-based operations.

        Commits on success, rolls back on exception.
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    def _ensure_tables(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self.connection() as conn:
            conn.executescript(_SCHEMA_SQL)
        logger.info("Database tables ensured at %s", self._path)

    def close(self) -> None:
        """Close the thread-local connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    intention TEXT NOT NULL,
    details TEXT,
    policy TEXT,
    total_calls INTEGER DEFAULT 0,
    approved_count INTEGER DEFAULT 0,
    denied_count INTEGER DEFAULT 0,
    escalated_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    call_id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    timestamp TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_redacted TEXT NOT NULL,
    classification TEXT NOT NULL,
    evaluation_path TEXT NOT NULL,
    decision TEXT NOT NULL,
    risk TEXT,
    reasoning TEXT,
    latency_ms INTEGER NOT NULL,
    user_decision TEXT,
    user_note TEXT,
    resolved_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_session
    ON audit_log(session_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_audit_decision
    ON audit_log(decision);
"""
