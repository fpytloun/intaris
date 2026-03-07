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
        """Create tables and indexes if they don't exist.

        Also runs schema migrations for columns added after initial release.
        """
        with self.connection() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._migrate(conn)
        logger.info("Database tables ensured at %s", self._path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Run schema migrations for columns added after initial release.

        Uses PRAGMA table_info to detect missing columns and adds them
        via ALTER TABLE. SQLite does not support ADD COLUMN IF NOT EXISTS,
        so we check first.
        """
        # Migration: add args_hash to audit_log (for MCP proxy escalation retry)
        if not self._column_exists(conn, "audit_log", "args_hash"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN args_hash TEXT")
            logger.info("Migration: added args_hash column to audit_log")

        # Migration: create escalation retry index (requires args_hash column)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_escalation_retry "
            "ON audit_log(user_id, session_id, tool, args_hash, user_decision)"
        )

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        """Check if a column exists in a table."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        return column in columns

    def close(self) -> None:
        """Close the thread-local connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    intention TEXT NOT NULL,
    details TEXT,
    policy TEXT,
    total_calls INTEGER DEFAULT 0,
    approved_count INTEGER DEFAULT 0,
    denied_count INTEGER DEFAULT 0,
    escalated_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    call_id TEXT UNIQUE NOT NULL,
    record_type TEXT NOT NULL DEFAULT 'tool_call',
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    timestamp TEXT NOT NULL,
    tool TEXT,
    args_redacted TEXT,
    content TEXT,
    classification TEXT,
    evaluation_path TEXT NOT NULL,
    decision TEXT NOT NULL,
    risk TEXT,
    reasoning TEXT,
    latency_ms INTEGER NOT NULL,
    user_decision TEXT,
    user_note TEXT,
    resolved_at TEXT,
    args_hash TEXT,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_user_id
    ON audit_log(user_id);

CREATE INDEX IF NOT EXISTS idx_audit_session
    ON audit_log(user_id, session_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_audit_decision
    ON audit_log(decision);

CREATE INDEX IF NOT EXISTS idx_audit_record_type
    ON audit_log(record_type);

-- MCP proxy: upstream server configurations (per-user)
CREATE TABLE IF NOT EXISTS mcp_servers (
    user_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    transport     TEXT NOT NULL,
    command       TEXT,
    args          TEXT,
    env_encrypted TEXT,
    cwd           TEXT,
    url           TEXT,
    headers_encrypted TEXT,
    agent_pattern TEXT NOT NULL DEFAULT '*',
    enabled       INTEGER NOT NULL DEFAULT 1,
    source        TEXT NOT NULL DEFAULT 'api',
    server_instructions TEXT,
    tools_cache   TEXT,
    tools_cache_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_user
    ON mcp_servers(user_id, enabled);

-- MCP proxy: per-tool preference overrides
CREATE TABLE IF NOT EXISTS mcp_tool_preferences (
    user_id     TEXT NOT NULL,
    server_name TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    preference  TEXT NOT NULL DEFAULT 'evaluate'
        CHECK (preference IN ('auto-approve', 'evaluate', 'escalate', 'deny')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, server_name, tool_name),
    FOREIGN KEY (user_id, server_name) REFERENCES mcp_servers(user_id, name)
        ON DELETE CASCADE
);
"""
