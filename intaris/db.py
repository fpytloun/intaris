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

        # Migration: add last_activity_at to sessions (behavioral guardrails)
        if not self._column_exists(conn, "sessions", "last_activity_at"):
            conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_at TEXT")
            # Backfill existing sessions with updated_at
            conn.execute(
                "UPDATE sessions SET last_activity_at = updated_at "
                "WHERE last_activity_at IS NULL"
            )
            logger.info("Migration: added last_activity_at column to sessions")

        # Migration: add parent_session_id to sessions (session continuation)
        if not self._column_exists(conn, "sessions", "parent_session_id"):
            conn.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
            logger.info("Migration: added parent_session_id column to sessions")

        # Migration: add summary_count to sessions (volume trigger tracking)
        if not self._column_exists(conn, "sessions", "summary_count"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN summary_count INTEGER DEFAULT 0"
            )
            logger.info("Migration: added summary_count column to sessions")

        # Migration: add profile_version to audit_log (profile traceability)
        if not self._column_exists(conn, "audit_log", "profile_version"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN profile_version INTEGER")
            logger.info("Migration: added profile_version column to audit_log")

        # Migration: add intention to audit_log (intention tracking per tool call)
        if not self._column_exists(conn, "audit_log", "intention"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN intention TEXT")
            logger.info("Migration: added intention column to audit_log")

        # Migration: add intention_source to sessions (tracks how intention
        # was set: "initial", "user", or "bootstrap")
        if not self._column_exists(conn, "sessions", "intention_source"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN "
                "intention_source TEXT DEFAULT 'initial'"
            )
            logger.info("Migration: added intention_source column to sessions")

        # Migration: update analysis_tasks CHECK constraint to include
        # 'intention_update'. SQLite doesn't support ALTER CHECK, so we
        # recreate the table. Task queue data is transient — completed
        # tasks are cleaned up periodically, so this is safe.
        self._migrate_analysis_tasks_check(conn)

        # Migration: add status_reason to sessions (explains why a session
        # was suspended/terminated, e.g., intention alignment failure)
        if not self._column_exists(conn, "sessions", "status_reason"):
            conn.execute("ALTER TABLE sessions ADD COLUMN status_reason TEXT")
            logger.info("Migration: added status_reason column to sessions")

        # Migration: add agent_id to sessions (global agent filter)
        if not self._column_exists(conn, "sessions", "agent_id"):
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
            # Backfill from the earliest audit_log record per session
            conn.execute(
                """
                UPDATE sessions SET agent_id = (
                    SELECT agent_id FROM audit_log
                    WHERE audit_log.user_id = sessions.user_id
                      AND audit_log.session_id = sessions.session_id
                      AND audit_log.agent_id IS NOT NULL
                    ORDER BY timestamp ASC
                    LIMIT 1
                ) WHERE agent_id IS NULL
                """
            )
            logger.info("Migration: added agent_id column to sessions (backfilled)")

        # Migration: add events column to notification_channels
        # (configurable event types per channel, JSON array)
        if not self._column_exists(conn, "notification_channels", "events"):
            conn.execute("ALTER TABLE notification_channels ADD COLUMN events TEXT")
            logger.info("Migration: added events column to notification_channels")

        # Migration: add alignment_overridden to sessions (persists user
        # acknowledgment of alignment misalignment across server restarts)
        if not self._column_exists(conn, "sessions", "alignment_overridden"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN alignment_overridden INTEGER DEFAULT 0"
            )
            logger.info("Migration: added alignment_overridden column to sessions")

        # Index for idle session sweep (status + last_activity_at)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_idle "
            "ON sessions(status, last_activity_at)"
        )

        # Index for agent_id filtering on audit_log
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(user_id, agent_id)"
        )

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        """Check if a column exists in a table."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        return column in columns

    @staticmethod
    def _migrate_analysis_tasks_check(conn: sqlite3.Connection) -> None:
        """Recreate analysis_tasks table if CHECK constraint is outdated.

        The original schema only allowed ('summary', 'analysis'). We need
        to include 'intention_update'. SQLite doesn't support ALTER CHECK,
        so we recreate the table with data migration.
        """
        # Check if the table exists
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='analysis_tasks'"
        )
        row = cursor.fetchone()
        if not row:
            return  # Table doesn't exist yet, CREATE TABLE will handle it

        create_sql = row[0]
        if "intention_update" in create_sql:
            return  # Already migrated

        logger.info(
            "Migration: recreating analysis_tasks table to update CHECK constraint"
        )
        conn.execute(
            """
            CREATE TABLE analysis_tasks_new (
                id              TEXT PRIMARY KEY,
                task_type       TEXT NOT NULL
                    CHECK (task_type IN ('summary', 'analysis', 'intention_update')),
                user_id         TEXT NOT NULL,
                session_id      TEXT,
                status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
                priority        INTEGER NOT NULL DEFAULT 0,
                payload         TEXT,
                result          TEXT,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                max_retries     INTEGER NOT NULL DEFAULT 3,
                next_attempt_at TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO analysis_tasks_new
            SELECT * FROM analysis_tasks
            """
        )
        conn.execute("DROP TABLE analysis_tasks")
        conn.execute("ALTER TABLE analysis_tasks_new RENAME TO analysis_tasks")
        # Recreate indexes (dropped with the old table)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_pending "
            "ON analysis_tasks(status, next_attempt_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user "
            "ON analysis_tasks(user_id, task_type, status)"
        )

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

-- Behavioral guardrails: Intaris-generated session summaries
CREATE TABLE IF NOT EXISTS session_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    trigger         TEXT NOT NULL
        CHECK (trigger IN ('inactivity', 'volume', 'close', 'manual')),
    summary         TEXT NOT NULL,
    tools_used      TEXT,
    intent_alignment TEXT NOT NULL
        CHECK (intent_alignment IN (
            'aligned', 'partially_aligned', 'misaligned', 'unclear'
        )),
    risk_indicators TEXT,
    call_count      INTEGER NOT NULL,
    approved_count  INTEGER NOT NULL DEFAULT 0,
    denied_count    INTEGER NOT NULL DEFAULT 0,
    escalated_count INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_summaries_user_time
    ON session_summaries(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_summaries_session
    ON session_summaries(user_id, session_id);

-- Behavioral guardrails: agent-reported summaries (untrusted, stored separately)
CREATE TABLE IF NOT EXISTS agent_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

-- Behavioral guardrails: cross-session analysis results
CREATE TABLE IF NOT EXISTS behavioral_analyses (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    analysis_type   TEXT NOT NULL
        CHECK (analysis_type IN ('session_end', 'periodic', 'on_demand')),
    sessions_scope  TEXT,
    risk_level      TEXT NOT NULL
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    findings        TEXT NOT NULL,
    recommendations TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_user_time
    ON behavioral_analyses(user_id, created_at);

-- Behavioral guardrails: pre-computed behavioral profiles (fast evaluator lookup)
CREATE TABLE IF NOT EXISTS behavioral_profiles (
    user_id         TEXT PRIMARY KEY,
    risk_level      TEXT NOT NULL DEFAULT 'low'
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    active_alerts   TEXT,
    context_summary TEXT,
    profile_version INTEGER NOT NULL DEFAULT 0,
    last_analysis_id TEXT,
    updated_at      TEXT NOT NULL
);

-- Behavioral guardrails: SQLite-backed task queue for background reliability
CREATE TABLE IF NOT EXISTS analysis_tasks (
    id              TEXT PRIMARY KEY,
    task_type       TEXT NOT NULL
        CHECK (task_type IN ('summary', 'analysis', 'intention_update')),
    user_id         TEXT NOT NULL,
    session_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    priority        INTEGER NOT NULL DEFAULT 0,
    payload         TEXT,
    result          TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending
    ON analysis_tasks(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_tasks_user
    ON analysis_tasks(user_id, task_type, status);

-- Per-user notification channels for escalation alerts
CREATE TABLE IF NOT EXISTS notification_channels (
    user_id          TEXT NOT NULL,
    name             TEXT NOT NULL,
    provider         TEXT NOT NULL,
    config_encrypted TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    events           TEXT,  -- JSON array of event types to receive (null = default set)
    last_success_at  TEXT,
    failure_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_notification_channels_user
    ON notification_channels(user_id, enabled);
"""
