"""Database connection management for intaris.

Supports SQLite (dev) and PostgreSQL (prod) backends. The backend is
selected via ``DBConfig.backend`` (``DB_BACKEND`` env var).

Both backends expose the same ``Database`` interface: ``connection()``
and ``cursor()`` context managers that return dict-like rows and accept
``?`` parameter placeholders. The PostgreSQL backend translates ``?``
to ``%s`` transparently so all SQL throughout the codebase works
unchanged.

SQLite uses WAL mode with thread-local connections. PostgreSQL uses
``psycopg2`` with a ``ThreadedConnectionPool``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Generator

from intaris.config import DBConfig

logger = logging.getLogger(__name__)


def _translate_placeholders(sql: str) -> str:
    """Translate ``?`` placeholders to ``%s`` for psycopg2.

    Simple replacement that works because none of the SQL in the
    codebase contains literal ``?`` characters in string constants.
    """
    return sql.replace("?", "%s")


# ── Row Wrapper ───────────────────────────────────────────────────────


class _DualAccessRow(dict):
    """Dict subclass that also supports integer indexing.

    ``sqlite3.Row`` supports both ``row["col"]`` and ``row[0]``.
    ``psycopg2.extras.RealDictRow`` only supports dict access.  This
    wrapper bridges the gap so all existing ``row[0]`` call sites work
    unchanged on PostgreSQL.
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


# ── Cursor Wrapper ────────────────────────────────────────────────────


class _PgCursorWrapper:
    """Wraps a psycopg2 cursor to translate ``?`` → ``%s`` and return dicts.

    This makes PostgreSQL cursors behave identically to SQLite cursors
    with ``sqlite3.Row`` factory — callers can use ``dict(row)`` and
    ``row[index]`` interchangeably.

    Additionally normalises rows so that ``datetime`` values (returned
    by psycopg2 for ``TIMESTAMPTZ`` columns) are converted to ISO 8601
    strings, matching the SQLite ``TEXT`` timestamp convention used
    throughout the codebase.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: str, params: Any = None) -> Any:
        translated = _translate_placeholders(sql)
        if params is not None:
            # Convert list to tuple for psycopg2
            if isinstance(params, list):
                params = tuple(params)
            return self._cursor.execute(translated, params)
        return self._cursor.execute(translated)

    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements (PostgreSQL version)."""
        self._cursor.execute(sql)

    @staticmethod
    def _normalize_row(row: Any) -> _DualAccessRow | None:
        """Convert a RealDictRow into a _DualAccessRow with ISO timestamps.

        - Wraps the row so it supports both ``row["col"]`` and ``row[0]``.
        - Converts ``datetime`` values to ISO 8601 strings so the rest of
          the codebase (Pydantic models, ``fromisoformat()`` calls, JSON
          serialisation) works identically to the SQLite backend.
        """
        if row is None:
            return None
        from datetime import datetime

        return _DualAccessRow(
            (k, v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()
        )

    def fetchone(self) -> _DualAccessRow | None:
        return self._normalize_row(self._cursor.fetchone())

    def fetchall(self) -> list[_DualAccessRow]:
        return [self._normalize_row(r) for r in self._cursor.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def close(self) -> None:
        self._cursor.close()


# ── Database Class ────────────────────────────────────────────────────


class Database:
    """Database manager supporting SQLite and PostgreSQL backends.

    Public API is identical for both backends:
    - ``connection()`` context manager: yields a connection, commits/rollbacks
    - ``cursor()`` context manager: yields a cursor, commits/rollbacks
    - ``backend`` property: "sqlite" or "postgresql"

    All SQL uses ``?`` placeholders. The PostgreSQL backend translates
    them to ``%s`` transparently.
    """

    def __init__(self, config: DBConfig):
        self._backend = config.backend

        if self._backend == "postgresql":
            self._init_postgresql(config)
        else:
            self._init_sqlite(config)

        self._ensure_tables()

    @property
    def backend(self) -> str:
        """Return the active backend name: "sqlite" or "postgresql"."""
        return self._backend

    # ── SQLite Backend ────────────────────────────────────────────

    def _init_sqlite(self, config: DBConfig) -> None:
        self._path = config.path
        self._local = threading.local()
        self._pool = None
        db_dir = os.path.dirname(self._path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    def _get_sqlite_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local SQLite connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    # ── PostgreSQL Backend ────────────────────────────────────────

    def _init_postgresql(self, config: DBConfig) -> None:
        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL backend. "
                "Install with: pip install intaris[postgresql]"
            ) from None

        self._path = ""
        self._local = threading.local()
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=config.database_url,
        )
        logger.info("PostgreSQL connection pool initialized")

    def _get_pg_connection(self) -> Any:
        """Get a connection from the PostgreSQL pool."""
        import psycopg2.extras

        conn = self._pool.getconn()
        # Use RealDictCursor so rows behave like dicts (same as sqlite3.Row)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn

    def _put_pg_connection(self, conn: Any) -> None:
        """Return a connection to the PostgreSQL pool."""
        self._pool.putconn(conn)

    # ── Public API ────────────────────────────────────────────────

    @contextmanager
    def connection(self) -> Generator[Any, None, None]:
        """Context manager for database operations.

        Commits on success, rolls back on exception.
        """
        if self._backend == "postgresql":
            conn = self._get_pg_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._put_pg_connection(conn)
        else:
            conn = self._get_sqlite_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def cursor(self) -> Generator[Any, None, None]:
        """Context manager for cursor-based operations.

        Commits on success, rolls back on exception.
        For PostgreSQL, wraps the cursor to translate ``?`` → ``%s``.
        """
        if self._backend == "postgresql":
            conn = self._get_pg_connection()
            try:
                raw_cursor = conn.cursor()
                wrapper = _PgCursorWrapper(raw_cursor)
                try:
                    yield wrapper
                finally:
                    raw_cursor.close()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._put_pg_connection(conn)
        else:
            with self.connection() as conn:
                cursor = conn.cursor()
                try:
                    yield cursor
                finally:
                    cursor.close()

    # ── Schema Management ─────────────────────────────────────────

    def _ensure_tables(self) -> None:
        """Create tables and indexes if they don't exist.

        Also runs schema migrations for columns added after initial release.
        """
        if self._backend == "postgresql":
            with self.connection() as conn:
                cur = conn.cursor()
                try:
                    cur.execute(_SCHEMA_SQL_PG)
                finally:
                    cur.close()
                self._migrate_pg(conn)
            logger.info("Database tables ensured (PostgreSQL)")
        else:
            with self.connection() as conn:
                conn.executescript(_SCHEMA_SQL_SQLITE)
                self._migrate_sqlite(conn)
            logger.info("Database tables ensured at %s", self._path)

    # ── SQLite Migrations ─────────────────────────────────────────

    def _migrate_sqlite(self, conn: sqlite3.Connection) -> None:
        """Run schema migrations for SQLite backend."""
        # Migration: add args_hash to audit_log
        if not self._sqlite_column_exists(conn, "audit_log", "args_hash"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN args_hash TEXT")
            logger.info("Migration: added args_hash column to audit_log")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_escalation_retry "
            "ON audit_log(user_id, session_id, tool, args_hash, user_decision)"
        )

        if not self._sqlite_column_exists(conn, "sessions", "last_activity_at"):
            conn.execute("ALTER TABLE sessions ADD COLUMN last_activity_at TEXT")
            conn.execute(
                "UPDATE sessions SET last_activity_at = updated_at "
                "WHERE last_activity_at IS NULL"
            )
            logger.info("Migration: added last_activity_at column to sessions")

        if not self._sqlite_column_exists(conn, "sessions", "parent_session_id"):
            conn.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
            logger.info("Migration: added parent_session_id column to sessions")

        if not self._sqlite_column_exists(conn, "sessions", "summary_count"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN summary_count INTEGER DEFAULT 0"
            )
            logger.info("Migration: added summary_count column to sessions")

        if not self._sqlite_column_exists(conn, "audit_log", "profile_version"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN profile_version INTEGER")
            logger.info("Migration: added profile_version column to audit_log")

        if not self._sqlite_column_exists(conn, "audit_log", "intention"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN intention TEXT")
            logger.info("Migration: added intention column to audit_log")

        if not self._sqlite_column_exists(conn, "sessions", "intention_source"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN "
                "intention_source TEXT DEFAULT 'initial'"
            )
            logger.info("Migration: added intention_source column to sessions")

        self._migrate_analysis_tasks_check_sqlite(conn)

        if not self._sqlite_column_exists(conn, "sessions", "status_reason"):
            conn.execute("ALTER TABLE sessions ADD COLUMN status_reason TEXT")
            logger.info("Migration: added status_reason column to sessions")

        if not self._sqlite_column_exists(conn, "sessions", "agent_id"):
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
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

        if not self._sqlite_column_exists(conn, "notification_channels", "events"):
            conn.execute("ALTER TABLE notification_channels ADD COLUMN events TEXT")
            logger.info("Migration: added events column to notification_channels")

        if not self._sqlite_column_exists(conn, "sessions", "alignment_overridden"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN alignment_overridden INTEGER DEFAULT 0"
            )
            logger.info("Migration: added alignment_overridden column to sessions")

        if not self._sqlite_column_exists(conn, "audit_log", "injection_detected"):
            conn.execute(
                "ALTER TABLE audit_log ADD COLUMN injection_detected INTEGER DEFAULT 0"
            )
            logger.info("Migration: added injection_detected column to audit_log")

        if not self._sqlite_column_exists(conn, "sessions", "last_alignment"):
            conn.execute("ALTER TABLE sessions ADD COLUMN last_alignment TEXT")
            logger.info("Migration: added last_alignment column to sessions")

        # Migration: add summary_type to session_summaries
        if not self._sqlite_column_exists(conn, "session_summaries", "summary_type"):
            conn.execute(
                "ALTER TABLE session_summaries ADD COLUMN "
                "summary_type TEXT DEFAULT 'window'"
            )
            logger.info("Migration: added summary_type column to session_summaries")

        # Migration: recreate session_summaries for updated CHECK constraints
        self._migrate_session_summaries_check_sqlite(conn)

        # Migration: add parent session index
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_parent "
            "ON sessions(user_id, parent_session_id)"
        )

        # Migration: add agent_id to behavioral_analyses
        if not self._sqlite_column_exists(conn, "behavioral_analyses", "agent_id"):
            conn.execute("ALTER TABLE behavioral_analyses ADD COLUMN agent_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analyses_agent "
                "ON behavioral_analyses(user_id, agent_id, created_at)"
            )
            logger.info("Migration: added agent_id column to behavioral_analyses")

        # Migration: add agent_id to behavioral_profiles (recreate with compound PK)
        self._migrate_behavioral_profiles_sqlite(conn)

        # Migration: convert risk_level from TEXT enum to INTEGER 1-10
        self._migrate_risk_level_numeric_sqlite(conn)

        # Migration: add judge columns to audit_log
        if not self._sqlite_column_exists(conn, "audit_log", "resolved_by"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN resolved_by TEXT")
            logger.info("Migration: added resolved_by column to audit_log")

        if not self._sqlite_column_exists(conn, "audit_log", "judge_reasoning"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN judge_reasoning TEXT")
            logger.info("Migration: added judge_reasoning column to audit_log")

        if not self._sqlite_column_exists(conn, "audit_log", "judge_decision"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN judge_decision TEXT")
            logger.info("Migration: added judge_decision column to audit_log")

        if not self._sqlite_column_exists(conn, "audit_log", "judge_risk"):
            conn.execute("ALTER TABLE audit_log ADD COLUMN judge_risk TEXT")
            logger.info("Migration: added judge_risk column to audit_log")

        if not self._sqlite_column_exists(conn, "sessions", "title"):
            conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
            logger.info("Migration: added title column to sessions")

        if not self._sqlite_column_exists(conn, "analysis_tasks", "started_at"):
            conn.execute("ALTER TABLE analysis_tasks ADD COLUMN started_at TEXT")
            logger.info("Migration: added started_at column to analysis_tasks")

        if not self._sqlite_column_exists(conn, "analysis_tasks", "heartbeat_at"):
            conn.execute("ALTER TABLE analysis_tasks ADD COLUMN heartbeat_at TEXT")
            logger.info("Migration: added heartbeat_at column to analysis_tasks")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_idle "
            "ON sessions(status, last_activity_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(user_id, agent_id)"
        )

    @staticmethod
    def _sqlite_column_exists(
        conn: sqlite3.Connection, table: str, column: str
    ) -> bool:
        """Check if a column exists in a SQLite table."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        return column in columns

    @staticmethod
    def _migrate_behavioral_profiles_sqlite(conn: sqlite3.Connection) -> None:
        """Recreate behavioral_profiles with compound PK (user_id, agent_id).

        The original table had user_id as the sole PK. The new schema uses
        (user_id, agent_id) for agent-scoped profiles. Since this table is
        typically empty (Phase 1 stubs never populated it), this migration
        is safe. Any existing rows get agent_id='' (the default).
        """
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='behavioral_profiles'"
        )
        row = cursor.fetchone()
        if not row:
            return
        create_sql = row[0]
        if "agent_id" in create_sql:
            return  # Already migrated

        logger.info(
            "Migration: recreating behavioral_profiles with compound PK "
            "(user_id, agent_id)"
        )
        conn.execute(
            """
            CREATE TABLE behavioral_profiles_new (
                user_id         TEXT NOT NULL,
                agent_id        TEXT NOT NULL DEFAULT '',
                risk_level      TEXT NOT NULL DEFAULT 'low'
                    CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
                active_alerts   TEXT,
                context_summary TEXT,
                profile_version INTEGER NOT NULL DEFAULT 0,
                last_analysis_id TEXT,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (user_id, agent_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO behavioral_profiles_new "
            "(user_id, agent_id, risk_level, active_alerts, context_summary, "
            "profile_version, last_analysis_id, updated_at) "
            "SELECT user_id, '', risk_level, active_alerts, context_summary, "
            "profile_version, last_analysis_id, updated_at "
            "FROM behavioral_profiles"
        )
        conn.execute("DROP TABLE behavioral_profiles")
        conn.execute(
            "ALTER TABLE behavioral_profiles_new RENAME TO behavioral_profiles"
        )

    @staticmethod
    def _migrate_risk_level_numeric_sqlite(conn: sqlite3.Connection) -> None:
        """Migrate risk_level from TEXT enum to INTEGER 1-10.

        Clears all analysis data (pre-release, no production data to
        preserve) and recreates behavioral_analyses and
        behavioral_profiles with INTEGER risk_level CHECK (1..10).
        Also clears session_summaries and agent_summaries since their
        risk_indicators JSON now uses numeric severity.
        """
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='behavioral_profiles'"
        )
        row = cursor.fetchone()
        if not row:
            return
        create_sql = row[0]
        if "BETWEEN 1 AND 10" in create_sql:
            return  # Already migrated

        logger.info(
            "Migration: converting risk_level to numeric 1-10 "
            "(clearing all analysis data)"
        )

        # Clear all analysis data — pre-release, regeneration is cheap
        conn.execute("DELETE FROM behavioral_analyses")
        conn.execute("DELETE FROM behavioral_profiles")
        conn.execute("DELETE FROM session_summaries")
        conn.execute("DELETE FROM agent_summaries")

        # Recreate behavioral_analyses with INTEGER risk_level
        conn.execute("DROP TABLE IF EXISTS behavioral_analyses_new")
        conn.execute(
            """
            CREATE TABLE behavioral_analyses_new (
                id              TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                agent_id        TEXT,
                analysis_type   TEXT NOT NULL
                    CHECK (analysis_type IN (
                        'session_end', 'periodic', 'on_demand'
                    )),
                sessions_scope  TEXT,
                risk_level      INTEGER NOT NULL DEFAULT 1
                    CHECK (risk_level BETWEEN 1 AND 10),
                findings        TEXT NOT NULL,
                recommendations TEXT,
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.execute("DROP TABLE behavioral_analyses")
        conn.execute(
            "ALTER TABLE behavioral_analyses_new RENAME TO behavioral_analyses"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analyses_user_time "
            "ON behavioral_analyses(user_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analyses_agent "
            "ON behavioral_analyses(user_id, agent_id, created_at)"
        )

        # Recreate behavioral_profiles with INTEGER risk_level
        conn.execute("DROP TABLE IF EXISTS behavioral_profiles_new")
        conn.execute(
            """
            CREATE TABLE behavioral_profiles_new (
                user_id         TEXT NOT NULL,
                agent_id        TEXT NOT NULL DEFAULT '',
                risk_level      INTEGER NOT NULL DEFAULT 1
                    CHECK (risk_level BETWEEN 1 AND 10),
                active_alerts   TEXT,
                context_summary TEXT,
                profile_version INTEGER NOT NULL DEFAULT 0,
                last_analysis_id TEXT,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (user_id, agent_id)
            )
            """
        )
        conn.execute("DROP TABLE behavioral_profiles")
        conn.execute(
            "ALTER TABLE behavioral_profiles_new RENAME TO behavioral_profiles"
        )

    @staticmethod
    def _migrate_analysis_tasks_check_sqlite(conn: sqlite3.Connection) -> None:
        """Recreate analysis_tasks table if CHECK constraint is outdated (SQLite)."""
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='analysis_tasks'"
        )
        row = cursor.fetchone()
        if not row:
            return
        create_sql = row[0]
        if "intention_update" in create_sql:
            return

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
                started_at      TEXT,
                heartbeat_at    TEXT,
                completed_at    TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO analysis_tasks_new "
            "(id, task_type, user_id, session_id, status, priority, payload, result, "
            "retry_count, max_retries, next_attempt_at, created_at, started_at, "
            "heartbeat_at, completed_at) "
            "SELECT id, task_type, user_id, session_id, status, priority, payload, "
            "result, retry_count, max_retries, next_attempt_at, created_at, NULL, "
            "NULL, completed_at FROM analysis_tasks"
        )
        conn.execute("DROP TABLE analysis_tasks")
        conn.execute("ALTER TABLE analysis_tasks_new RENAME TO analysis_tasks")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_pending "
            "ON analysis_tasks(status, next_attempt_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user "
            "ON analysis_tasks(user_id, task_type, status)"
        )

    @staticmethod
    def _migrate_session_summaries_check_sqlite(conn: sqlite3.Connection) -> None:
        """Recreate session_summaries if CHECK constraints are outdated (SQLite).

        Adds 'compaction' to the trigger CHECK and ensures summary_type
        CHECK is present. Uses the same table-recreation pattern as
        analysis_tasks migration.
        """
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='session_summaries'"
        )
        row = cursor.fetchone()
        if not row:
            return
        create_sql = row[0]
        # Already up to date if 'compaction' is in the CHECK constraint
        if "compaction" in create_sql:
            return

        logger.info(
            "Migration: recreating session_summaries table to update "
            "CHECK constraints (trigger + summary_type)"
        )
        # Drop leftover temp table from a previously failed migration attempt
        conn.execute("DROP TABLE IF EXISTS session_summaries_new")
        conn.execute(
            """
            CREATE TABLE session_summaries_new (
                id              TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                session_id      TEXT NOT NULL,
                window_start    TEXT NOT NULL,
                window_end      TEXT NOT NULL,
                trigger         TEXT NOT NULL
                    CHECK (trigger IN (
                        'inactivity', 'volume', 'close', 'manual', 'compaction'
                    )),
                summary_type    TEXT NOT NULL DEFAULT 'window'
                    CHECK (summary_type IN ('window', 'compacted')),
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
                FOREIGN KEY (user_id, session_id)
                    REFERENCES sessions(user_id, session_id)
            )
            """
        )
        # Copy data — summary_type column may or may not exist in old table.
        # When it exists, existing rows may have NULL (ALTER TABLE ADD COLUMN
        # DEFAULT only applies to new inserts in SQLite), so use COALESCE.
        old_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(session_summaries)").fetchall()
        }
        if "summary_type" in old_cols:
            conn.execute(
                "INSERT INTO session_summaries_new "
                "(id, user_id, session_id, window_start, window_end, "
                "trigger, summary_type, summary, tools_used, "
                "intent_alignment, risk_indicators, call_count, "
                "approved_count, denied_count, escalated_count, created_at) "
                "SELECT id, user_id, session_id, window_start, window_end, "
                "trigger, COALESCE(summary_type, 'window'), summary, "
                "tools_used, intent_alignment, risk_indicators, call_count, "
                "approved_count, denied_count, escalated_count, created_at "
                "FROM session_summaries"
            )
        else:
            conn.execute(
                "INSERT INTO session_summaries_new "
                "(id, user_id, session_id, window_start, window_end, "
                "trigger, summary_type, summary, tools_used, "
                "intent_alignment, risk_indicators, call_count, "
                "approved_count, denied_count, escalated_count, created_at) "
                "SELECT id, user_id, session_id, window_start, window_end, "
                "trigger, 'window', summary, tools_used, "
                "intent_alignment, risk_indicators, call_count, "
                "approved_count, denied_count, escalated_count, created_at "
                "FROM session_summaries"
            )
        conn.execute("DROP TABLE session_summaries")
        conn.execute("ALTER TABLE session_summaries_new RENAME TO session_summaries")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_user_time "
            "ON session_summaries(user_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_session "
            "ON session_summaries(user_id, session_id)"
        )

    # ── PostgreSQL Migrations ─────────────────────────────────────

    def _migrate_pg(self, conn: Any) -> None:
        """Run schema migrations for PostgreSQL backend.

        PostgreSQL supports ADD COLUMN IF NOT EXISTS, so migrations
        are simpler than SQLite.
        """
        cur = conn.cursor()
        try:
            # All columns that may need adding (idempotent with IF NOT EXISTS)
            migrations = [
                ("audit_log", "args_hash", "TEXT"),
                ("sessions", "last_activity_at", "TIMESTAMPTZ"),
                ("sessions", "parent_session_id", "TEXT"),
                ("sessions", "summary_count", "INTEGER DEFAULT 0"),
                ("audit_log", "profile_version", "INTEGER"),
                ("audit_log", "intention", "TEXT"),
                ("sessions", "intention_source", "TEXT DEFAULT 'initial'"),
                ("sessions", "status_reason", "TEXT"),
                ("sessions", "agent_id", "TEXT"),
                ("notification_channels", "events", "TEXT"),
                ("sessions", "alignment_overridden", "BOOLEAN DEFAULT FALSE"),
                ("sessions", "last_alignment", "TEXT"),
                ("audit_log", "injection_detected", "BOOLEAN DEFAULT FALSE"),
                ("behavioral_analyses", "agent_id", "TEXT"),
                ("behavioral_profiles", "agent_id", "TEXT NOT NULL DEFAULT ''"),
                ("session_summaries", "summary_type", "TEXT NOT NULL DEFAULT 'window'"),
                ("audit_log", "resolved_by", "TEXT"),
                ("audit_log", "judge_reasoning", "TEXT"),
                ("audit_log", "judge_decision", "TEXT"),
                ("audit_log", "judge_risk", "TEXT"),
                ("sessions", "title", "TEXT"),
                ("analysis_tasks", "started_at", "TIMESTAMPTZ"),
                ("analysis_tasks", "heartbeat_at", "TIMESTAMPTZ"),
            ]
            for table, column, col_type in migrations:
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
                )

            # Backfill last_activity_at from updated_at where null
            cur.execute(
                "UPDATE sessions SET last_activity_at = updated_at "
                "WHERE last_activity_at IS NULL"
            )

            # Backfill agent_id from audit_log where null
            cur.execute(
                """
                UPDATE sessions SET agent_id = sub.agent_id
                FROM (
                    SELECT DISTINCT ON (user_id, session_id)
                        user_id, session_id, agent_id
                    FROM audit_log
                    WHERE agent_id IS NOT NULL
                    ORDER BY user_id, session_id, timestamp ASC
                ) sub
                WHERE sessions.user_id = sub.user_id
                  AND sessions.session_id = sub.session_id
                  AND sessions.agent_id IS NULL
                """
            )

            # Create indexes (IF NOT EXISTS works in PostgreSQL)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_escalation_retry "
                "ON audit_log(user_id, session_id, tool, args_hash, user_decision)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_idle "
                "ON sessions(status, last_activity_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_agent "
                "ON audit_log(user_id, agent_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_analyses_agent "
                "ON behavioral_analyses(user_id, agent_id, created_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_parent "
                "ON sessions(user_id, parent_session_id)"
            )

            # Migration: update session_summaries trigger CHECK constraint
            # to include 'compaction'. PostgreSQL requires DROP + ADD.
            try:
                cur.execute(
                    "ALTER TABLE session_summaries "
                    "DROP CONSTRAINT IF EXISTS session_summaries_trigger_check"
                )
                cur.execute(
                    "ALTER TABLE session_summaries "
                    "ADD CONSTRAINT session_summaries_trigger_check "
                    "CHECK (trigger IN ("
                    "'inactivity', 'volume', 'close', 'manual', 'compaction'"
                    "))"
                )
            except Exception:
                # Constraint may already be correct or table may not exist yet
                pass

            # Migration: convert risk_level from TEXT enum to INTEGER 1-10
            try:
                cur.execute(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'behavioral_profiles' "
                    "AND column_name = 'risk_level'"
                )
                col_row = cur.fetchone()
                if col_row and col_row["data_type"].lower() != "integer":
                    logger.info("Migration (PG): converting risk_level to numeric 1-10")
                    # Clear all analysis data — pre-release
                    cur.execute("DELETE FROM behavioral_analyses")
                    cur.execute("DELETE FROM behavioral_profiles")
                    cur.execute("DELETE FROM session_summaries")
                    cur.execute("DELETE FROM agent_summaries")
                    # Alter column types — must drop CHECK and DEFAULT
                    # before the TEXT→INTEGER cast, then re-add them.
                    for tbl in ("behavioral_analyses", "behavioral_profiles"):
                        cur.execute(
                            f"ALTER TABLE {tbl} "
                            f"DROP CONSTRAINT IF EXISTS {tbl}_risk_level_check"
                        )
                        cur.execute(
                            f"ALTER TABLE {tbl} ALTER COLUMN risk_level DROP DEFAULT"
                        )
                        cur.execute(
                            f"ALTER TABLE {tbl} "
                            "ALTER COLUMN risk_level TYPE INTEGER "
                            "USING 1"
                        )
                        cur.execute(
                            f"ALTER TABLE {tbl} ALTER COLUMN risk_level SET DEFAULT 1"
                        )
                        cur.execute(
                            f"ALTER TABLE {tbl} "
                            f"ADD CONSTRAINT {tbl}_risk_level_check "
                            "CHECK (risk_level BETWEEN 1 AND 10)"
                        )
            except Exception:
                logger.warning(
                    "PG migration for numeric risk_level failed",
                    exc_info=True,
                )

            # Migration: ensure behavioral_profiles has compound PK
            # (user_id, agent_id). Older PG installs may have PK on
            # user_id only (agent_id was added via ALTER TABLE ADD
            # COLUMN, which doesn't update the PK). The compound PK
            # is required for ON CONFLICT (user_id, agent_id) upserts.
            try:
                cur.execute(
                    "SELECT constraint_name FROM information_schema."
                    "table_constraints WHERE table_name = "
                    "'behavioral_profiles' AND constraint_type = "
                    "'PRIMARY KEY'"
                )
                pk_row = cur.fetchone()
                if pk_row:
                    pk_name = (
                        pk_row["constraint_name"]
                        if isinstance(pk_row, dict)
                        else pk_row[0]
                    )
                    # Check if agent_id is in the PK columns
                    cur.execute(
                        "SELECT column_name FROM information_schema."
                        "key_column_usage WHERE constraint_name = %s "
                        "AND table_name = 'behavioral_profiles'",
                        (pk_name,),
                    )
                    pk_cols = {
                        (r["column_name"] if isinstance(r, dict) else r[0])
                        for r in cur.fetchall()
                    }
                    if "agent_id" not in pk_cols:
                        logger.info(
                            "Migration (PG): adding agent_id to behavioral_profiles PK"
                        )
                        cur.execute(
                            f"ALTER TABLE behavioral_profiles DROP CONSTRAINT {pk_name}"
                        )
                        cur.execute(
                            "ALTER TABLE behavioral_profiles "
                            "ADD PRIMARY KEY (user_id, agent_id)"
                        )
            except Exception:
                logger.warning(
                    "PG migration for behavioral_profiles compound PK failed",
                    exc_info=True,
                )
        finally:
            cur.close()

    # ── Cleanup ───────────────────────────────────────────────────

    def close(self) -> None:
        """Close connections."""
        if self._backend == "postgresql":
            if self._pool is not None:
                self._pool.closeall()
                logger.info("PostgreSQL connection pool closed")
        else:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    # ── Backward compatibility ────────────────────────────────────

    # Keep _column_exists as a static method for any external callers.
    @staticmethod
    def _column_exists(conn: Any, table: str, column: str) -> bool:
        """Check if a column exists (SQLite only, kept for compatibility)."""
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        return column in columns


# ── SQLite Schema ─────────────────────────────────────────────────────

_SCHEMA_SQL_SQLITE = """
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
    resolved_by TEXT,
    judge_reasoning TEXT,
    judge_decision TEXT,
    judge_risk TEXT,
    args_hash TEXT,
    injection_detected INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS event_append_idempotency (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'completed')),
    count INTEGER,
    first_seq INTEGER,
    last_seq INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id, idempotency_key),
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_append_idempotency_created_at
    ON event_append_idempotency(created_at);

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
        CHECK (trigger IN ('inactivity', 'volume', 'close', 'manual', 'compaction')),
    summary_type    TEXT NOT NULL DEFAULT 'window'
        CHECK (summary_type IN ('window', 'compacted')),
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
-- idx_sessions_parent created by migration (after parent_session_id column is added)

-- Behavioral guardrails: agent-reported summaries (untrusted, stored separately)
CREATE TABLE IF NOT EXISTS agent_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

-- Behavioral guardrails: cross-session analysis results (agent-scoped)
CREATE TABLE IF NOT EXISTS behavioral_analyses (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    agent_id        TEXT,
    analysis_type   TEXT NOT NULL
        CHECK (analysis_type IN ('session_end', 'periodic', 'on_demand')),
    sessions_scope  TEXT,
    risk_level      INTEGER NOT NULL DEFAULT 1
        CHECK (risk_level BETWEEN 1 AND 10),
    findings        TEXT NOT NULL,
    recommendations TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_user_time
    ON behavioral_analyses(user_id, created_at);
-- idx_analyses_agent created by migration (after agent_id column is added)

-- Behavioral guardrails: pre-computed behavioral profiles (agent-scoped, fast evaluator lookup)
CREATE TABLE IF NOT EXISTS behavioral_profiles (
    user_id         TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    risk_level      INTEGER NOT NULL DEFAULT 1
        CHECK (risk_level BETWEEN 1 AND 10),
    active_alerts   TEXT,
    context_summary TEXT,
    profile_version INTEGER NOT NULL DEFAULT 0,
    last_analysis_id TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, agent_id)
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
    started_at      TEXT,
    heartbeat_at    TEXT,
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

-- Exchange token SSO: server-side sessions (multi-worker safe)
CREATE TABLE IF NOT EXISTS exchange_sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    agent_id    TEXT,
    expires_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exchange_sessions_expires
    ON exchange_sessions(expires_at);

-- Exchange token SSO: consumed JTIs for single-use enforcement
CREATE TABLE IF NOT EXISTS consumed_jtis (
    jti         TEXT PRIMARY KEY,
    expires_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consumed_jtis_expires
    ON consumed_jtis(expires_at);
"""


# ── PostgreSQL Schema ─────────────────────────────────────────────────

_SCHEMA_SQL_PG = """
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
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_activity_at TIMESTAMPTZ,
    parent_session_id TEXT,
    summary_count INTEGER DEFAULT 0,
    intention_source TEXT DEFAULT 'initial',
    status_reason TEXT,
    agent_id TEXT,
    alignment_overridden BOOLEAN DEFAULT FALSE,
    last_alignment TEXT,
    title TEXT,
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    call_id TEXT UNIQUE NOT NULL,
    record_type TEXT NOT NULL DEFAULT 'tool_call',
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
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
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    judge_reasoning TEXT,
    judge_decision TEXT,
    judge_risk TEXT,
    args_hash TEXT,
    profile_version INTEGER,
    intention TEXT,
    injection_detected BOOLEAN DEFAULT FALSE,
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

CREATE TABLE IF NOT EXISTS event_append_idempotency (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'completed')),
    count INTEGER,
    first_seq INTEGER,
    last_seq INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id, idempotency_key),
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_append_idempotency_created_at
    ON event_append_idempotency(created_at);

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
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    source        TEXT NOT NULL DEFAULT 'api',
    server_instructions TEXT,
    tools_cache   TEXT,
    tools_cache_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_user
    ON mcp_servers(user_id, enabled);

CREATE TABLE IF NOT EXISTS mcp_tool_preferences (
    user_id     TEXT NOT NULL,
    server_name TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    preference  TEXT NOT NULL DEFAULT 'evaluate'
        CHECK (preference IN ('auto-approve', 'evaluate', 'escalate', 'deny')),
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, server_name, tool_name),
    FOREIGN KEY (user_id, server_name) REFERENCES mcp_servers(user_id, name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    trigger         TEXT NOT NULL
        CHECK (trigger IN ('inactivity', 'volume', 'close', 'manual', 'compaction')),
    summary_type    TEXT NOT NULL DEFAULT 'window'
        CHECK (summary_type IN ('window', 'compacted')),
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
    created_at      TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_summaries_user_time
    ON session_summaries(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_summaries_session
    ON session_summaries(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(user_id, parent_session_id);

CREATE TABLE IF NOT EXISTS agent_summaries (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, session_id)
);

CREATE TABLE IF NOT EXISTS behavioral_analyses (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    agent_id        TEXT,
    analysis_type   TEXT NOT NULL
        CHECK (analysis_type IN ('session_end', 'periodic', 'on_demand')),
    sessions_scope  TEXT,
    risk_level      INTEGER NOT NULL DEFAULT 1
        CHECK (risk_level BETWEEN 1 AND 10),
    findings        TEXT NOT NULL,
    recommendations TEXT,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_user_time
    ON behavioral_analyses(user_id, created_at);
-- idx_analyses_agent created by migration (after agent_id column is added)

CREATE TABLE IF NOT EXISTS behavioral_profiles (
    user_id         TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    risk_level      INTEGER NOT NULL DEFAULT 1
        CHECK (risk_level BETWEEN 1 AND 10),
    active_alerts   TEXT,
    context_summary TEXT,
    profile_version INTEGER NOT NULL DEFAULT 0,
    last_analysis_id TEXT,
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, agent_id)
);

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
    next_attempt_at TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    started_at      TIMESTAMPTZ,
    heartbeat_at    TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_pending
    ON analysis_tasks(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_tasks_user
    ON analysis_tasks(user_id, task_type, status);

CREATE TABLE IF NOT EXISTS notification_channels (
    user_id          TEXT NOT NULL,
    name             TEXT NOT NULL,
    provider         TEXT NOT NULL,
    config_encrypted TEXT,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    events           TEXT,
    last_success_at  TIMESTAMPTZ,
    failure_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_notification_channels_user
    ON notification_channels(user_id, enabled);

-- Exchange token SSO: server-side sessions (multi-worker safe)
CREATE TABLE IF NOT EXISTS exchange_sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    agent_id    TEXT,
    expires_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exchange_sessions_expires
    ON exchange_sessions(expires_at);

-- Exchange token SSO: consumed JTIs for single-use enforcement
CREATE TABLE IF NOT EXISTS consumed_jtis (
    jti         TEXT PRIMARY KEY,
    expires_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consumed_jtis_expires
    ON consumed_jtis(expires_at);
"""
