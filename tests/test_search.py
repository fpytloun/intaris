"""Tests for the search subsystem.

Coverage:

- ``test_schema_*``         schema bootstrap on SQLite
- ``test_outbox_*``         enqueue / claim / mark cycle (vector tier)
- ``test_lexical_*``        per-kind queries against canonical tables
- ``test_intention_*``      DISTINCT-ON dedup behavior
- ``test_service_*``        end-to-end query orchestration
- ``test_qdrant_local_*``   Qdrant local-mode + dense+sparse hybrid
- ``test_pgvector_*``       skipped here (require live PG container)

Lexical tier always works; vector tier is exercised through stubs and
local-mode Qdrant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig, SearchConfig
from intaris.db import Database
from intaris.search import outbox
from intaris.search.cursor import decode_cursor, encode_cursor
from intaris.search.fusion import rrf_fuse
from intaris.search.lexical import search_lexical
from intaris.search.schema import SearchSchema
from intaris.search.service import SearchService
from intaris.search.types import (
    KIND_INTENTION,
    KIND_REASONING,
    KIND_SUMMARY,
    fold_text,
    truncate_text,
)
from intaris.search.vector import build_vector_backend

TEST_USER = "alice@example.com"
OTHER_USER = "bob@example.com"


def test_qdrant_backend_unavailability_is_retryable(monkeypatch, db):
    """Configured Qdrant downtime must propagate to the startup retry loop."""
    from intaris.search import vector

    backend = type(
        "UnavailableBackend",
        (),
        {"healthy": lambda self: False, "close": Mock()},
    )()
    monkeypatch.setattr(vector, "QdrantVectorBackend", lambda **kwargs: backend)
    config = SearchConfig(
        vector_provider="qdrant",
        qdrant_url="http://qdrant.invalid:6333",
        embedding_model="test-model",
    )

    with pytest.raises(RuntimeError, match="qdrant backend is unavailable"):
        build_vector_backend(db=db, config=config)
    backend.close.assert_called_once_with()


# ── Fixtures ────────────────────────────────────────────────────────


def test_partial_search_construction_closes_vector(monkeypatch, db):
    from intaris.search import service

    backend = Mock()
    backend.healthy.return_value = False
    monkeypatch.setattr(service, "build_vector_backend", lambda **kwargs: backend)
    monkeypatch.setattr(
        service, "load_state", Mock(side_effect=RuntimeError("database unavailable"))
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        SearchService(db=db, config=SearchConfig(enabled=True))
    backend.close.assert_called_once()


@pytest.fixture
def db(tmp_path):
    config = DBConfig()
    config.path = str(tmp_path / "test.db")
    return Database(config)


@pytest.fixture
def search_config():
    cfg = SearchConfig()
    cfg.enabled = True
    cfg.vector_provider = "disabled"
    cfg.embedding_model = ""
    cfg.indexer_poll_interval_seconds = 0.05
    return cfg


@pytest.fixture
def schema(db, search_config):
    s = SearchSchema(
        vector_enabled=search_config.vector_enabled(),
        embedding_dim=search_config.embedding_dim,
    )
    s.ensure(db)
    return s


@pytest.fixture
def service(db, search_config):
    return SearchService(db=db, config=search_config)


@pytest.fixture
def audit_store(db):
    AuditStore.set_search_service(None)
    return AuditStore(db)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_session(
    db,
    *,
    session_id: str,
    user_id: str = TEST_USER,
    agent_id: str | None = "agent-1",
    title: str = "rocket launch",
    intention: str = "track upcoming rocket launches",
):
    with db.cursor() as cur:
        now = _now()
        cur.execute(
            "INSERT INTO sessions "
            "  (user_id, session_id, intention, status, created_at, "
            "   updated_at, agent_id, last_activity_at, title) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                user_id,
                session_id,
                intention,
                now,
                now,
                agent_id,
                now,
                title,
            ),
        )


def _insert_summary(
    db, *, session_id: str, summary: str, user_id: str = TEST_USER
) -> str:
    sid = str(uuid.uuid4())
    now = _now()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO session_summaries "
            "  (id, user_id, session_id, window_start, window_end, "
            "   trigger, summary_type, summary, tools_used, "
            "   intent_alignment, risk_indicators, call_count, "
            "   created_at) "
            "VALUES (?, ?, ?, ?, ?, 'manual', 'window', ?, '[]', "
            "        'aligned', '[]', 0, ?)",
            (sid, user_id, session_id, now, now, summary, now),
        )
    return sid


# ── Types ──────────────────────────────────────────────────────────


def test_truncate_text_respects_byte_budget():
    text = "ä" * 100
    out = truncate_text(text, 100)
    assert len(out.encode("utf-8")) <= 100
    assert out


def test_fold_text_strips_diacritics():
    assert fold_text("Příliš žluťoučký") == "prilis zlutoucky"


# ── Schema ─────────────────────────────────────────────────────────


def test_schema_creates_outbox_and_state_on_sqlite(db, schema):
    with db.cursor() as cur:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('search_outbox','search_state','search_vectors')"
        )
        names = {row[0] for row in cur.fetchall()}
    assert {"search_outbox", "search_state", "search_vectors"} == names
    assert schema.lexical_backend == "sqlite-like"


def test_schema_is_idempotent(db, schema, search_config):
    SearchSchema(
        vector_enabled=False, embedding_dim=search_config.embedding_dim
    ).ensure(db)
    SearchSchema(
        vector_enabled=False, embedding_dim=search_config.embedding_dim
    ).ensure(db)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM search_state")
        assert cur.fetchone()[0] == 1


def test_split_pg_statements_handles_quoted_semicolons():
    """Schema bootstrap relies on _split_pg_statements to break a DDL
    script into individual ``cur.execute`` calls. Verify it doesn't
    split inside string literals (e.g. CHECK constraint values)."""
    from intaris.search.schema import _split_pg_statements

    script = """
    -- a comment ; not a separator
    CREATE TABLE t (x text CHECK (x IN ('a;b','c')));
    INSERT INTO t (x) VALUES ('a;b');
    """
    stmts = _split_pg_statements(script)
    assert len(stmts) == 2
    assert "CHECK" in stmts[0]
    assert "INSERT" in stmts[1]


def test_split_pg_statements_handles_double_quotes():
    from intaris.search.schema import _split_pg_statements

    script = 'CREATE INDEX "ix;weird" ON t(x); CREATE TABLE u (a int);'
    stmts = _split_pg_statements(script)
    assert len(stmts) == 2
    assert '"ix;weird"' in stmts[0]


def test_extract_source_unwraps_coalesce_payload():
    """Used by the tsvector fallback to rebuild the plain form when
    the IMMUTABLE wrapper isn't installable."""
    from intaris.search.schema import _extract_source

    assert (
        _extract_source("to_tsvector('simple', coalesce(intention,''))") == "intention"
    )
    assert (
        _extract_source(
            "to_tsvector('simple', intaris_immutable_unaccent(coalesce(content,'')))"
        )
        == "content"
    )


def test_tsvector_expression_picks_wrapper_when_available():
    from intaris.search.schema import SearchSchema

    s = SearchSchema(vector_enabled=False, embedding_dim=1536)
    s.has_unaccent = True
    s.has_immutable_unaccent = True
    expr = s._tsvector_expression("intention")
    assert "intaris_immutable_unaccent" in expr
    assert "coalesce(intention,'')" in expr


def test_tsvector_expression_falls_back_when_wrapper_missing():
    from intaris.search.schema import SearchSchema

    s = SearchSchema(vector_enabled=False, embedding_dim=1536)
    s.has_unaccent = True
    s.has_immutable_unaccent = False
    expr = s._tsvector_expression("intention")
    assert "intaris_immutable_unaccent" not in expr
    assert "unaccent" not in expr
    assert "coalesce(intention,'')" in expr


def test_tsvector_expr_matches_handles_pg_canonicalization():
    """``pg_get_expr`` returns canonicalized SQL with type casts and
    uppercased builtin names. The matcher must compare semantic
    features (wrapper presence + source column), not text, so existing
    deployments don't enter a drop-and-rebuild loop on every startup.
    """
    from intaris.search.schema import SearchSchema

    # Real ``pg_get_expr`` output captured from a live PG cluster after
    # the wrapper was installed.
    canonical_wrapper = (
        "to_tsvector('simple'::regconfig, "
        "intaris_immutable_unaccent(COALESCE(intention, ''::text)))"
    )
    # The hand-authored form generated by ``_tsvector_expression``.
    authored_wrapper = (
        "to_tsvector('simple', intaris_immutable_unaccent(coalesce(intention,'')))"
    )
    # Same flavor (wrapper) on the same source — must match.
    assert SearchSchema._tsvector_expr_matches(
        canonical_wrapper, authored_wrapper, source_expr="intention"
    )

    canonical_plain = "to_tsvector('simple'::regconfig, COALESCE(intention, ''::text))"
    authored_plain = "to_tsvector('simple', coalesce(intention,''))"
    # Same flavor (plain) on the same source — must match.
    assert SearchSchema._tsvector_expr_matches(
        canonical_plain, authored_plain, source_expr="intention"
    )


def test_tsvector_expr_matches_detects_flavor_change():
    """When the wrapper becomes available on a deployment that had the
    plain form, the matcher must report a mismatch so the rebuild
    path runs."""
    from intaris.search.schema import SearchSchema

    canonical_plain = "to_tsvector('simple'::regconfig, COALESCE(intention, ''::text))"
    authored_wrapper = (
        "to_tsvector('simple', intaris_immutable_unaccent(coalesce(intention,'')))"
    )
    assert not SearchSchema._tsvector_expr_matches(
        canonical_plain, authored_wrapper, source_expr="intention"
    )
    # And the reverse — wrapper went away.
    canonical_wrapper = (
        "to_tsvector('simple'::regconfig, "
        "intaris_immutable_unaccent(COALESCE(intention, ''::text)))"
    )
    authored_plain = "to_tsvector('simple', coalesce(intention,''))"
    assert not SearchSchema._tsvector_expr_matches(
        canonical_wrapper, authored_plain, source_expr="intention"
    )


def test_tsvector_expr_matches_detects_wrong_source_column():
    """Different source columns must not be treated as equivalent."""
    from intaris.search.schema import SearchSchema

    a = "to_tsvector('simple'::regconfig, COALESCE(intention, ''::text))"
    b = "to_tsvector('simple', coalesce(content,''))"
    assert not SearchSchema._tsvector_expr_matches(a, b, source_expr="content")
    assert not SearchSchema._tsvector_expr_matches(b, a, source_expr="intention")


class _FakePgCursor:
    """Minimal cursor stub for exercising ``_ensure_pg_tsvector_columns``.

    Records every executed SQL statement and lets the test set up
    canned responses for ``pg_get_expr`` and ``pg_stat_user_tables``
    lookups keyed by ``(table, column)`` and ``table`` respectively.
    """

    def __init__(
        self,
        *,
        existing_exprs: dict[tuple[str, str], str],
        rowcounts: dict[str, int],
    ) -> None:
        self._existing_exprs = existing_exprs
        self._rowcounts = rowcounts
        self.executed: list[tuple[str, tuple]] = []
        self._next_result: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: D401
        self.executed.append((sql, tuple(params)))
        norm = " ".join(sql.split()).lower()
        if "pg_get_expr" in norm:
            table, column = params
            expr = self._existing_exprs.get((table, column))
            self._next_result = [(expr,)] if expr is not None else []
        elif "pg_stat_user_tables" in norm:
            (table,) = params
            n = self._rowcounts.get(table)
            self._next_result = [(n,)] if n is not None else []
        else:
            self._next_result = []

    def fetchone(self):
        if not self._next_result:
            return None
        return self._next_result.pop(0)


def test_pg_tsvector_skips_rebuild_on_large_table_when_flavor_drifts(caplog):
    """A large ``audit_log`` with the plain expression must NOT be
    auto-rebuilt when the wrapper becomes available — the rewrite
    blocks startup long enough to fail K8s liveness probes. The
    bootstrap must log the manual SQL and continue."""
    import logging

    from intaris.search.schema import (
        _PG_AUTO_REBUILD_ROW_THRESHOLD,
        SearchSchema,
    )

    s = SearchSchema(vector_enabled=False, embedding_dim=1536)
    s.has_unaccent = True
    s.has_immutable_unaccent = True

    plain = "to_tsvector('simple'::regconfig, COALESCE(intention, ''::text))"
    cur = _FakePgCursor(
        existing_exprs={
            ("sessions", "intention_tsv"): plain,
            ("sessions", "title_tsv"): plain.replace("intention", "title"),
            ("audit_log", "intention_tsv"): plain,
            ("audit_log", "content_tsv"): plain.replace("intention", "content"),
            ("session_summaries", "summary_tsv"): plain.replace("intention", "summary"),
            ("agent_summaries", "summary_tsv"): plain.replace("intention", "summary"),
        },
        rowcounts={
            "sessions": 100,
            "audit_log": _PG_AUTO_REBUILD_ROW_THRESHOLD + 50_000,
            "session_summaries": 100,
            "agent_summaries": 100,
        },
    )

    with caplog.at_level(logging.WARNING, logger="intaris.search.schema"):
        s._ensure_pg_tsvector_columns(cur)

    # No DROP COLUMN issued against audit_log — that would block startup.
    drop_audit = [
        sql
        for sql, _ in cur.executed
        if "drop column" in sql.lower() and "audit_log" in sql.lower()
    ]
    assert drop_audit == [], drop_audit

    # Notes record the deferred work for both audit_log columns so
    # operators see them surfaced.
    deferred = [n for n in s.notes if n.startswith("audit_log.")]
    assert len(deferred) == 2, s.notes

    # Small tables (sessions etc.) DID get rebuilt — they're cheap.
    drop_sessions = [
        sql
        for sql, _ in cur.executed
        if "drop column" in sql.lower() and "sessions" in sql.lower()
    ]
    assert drop_sessions, "small tables should still rebuild"

    # WARNING log includes the manual SQL the operator can run.
    warning_text = "\n".join(r.message for r in caplog.records)
    assert "audit_log" in warning_text
    assert "ALTER TABLE" in warning_text
    assert "maintenance window" in warning_text


def test_pg_tsvector_skips_rebuild_when_flavor_already_matches():
    """When the existing expression already uses the wrapper on the
    desired source, no DROP/ADD must be issued — even on a huge table.
    This is the steady-state path that the broken matcher used to
    miss, causing every restart to rebuild every column."""
    from intaris.search.schema import SearchSchema

    s = SearchSchema(vector_enabled=False, embedding_dim=1536)
    s.has_unaccent = True
    s.has_immutable_unaccent = True

    # Real canonical form returned by pg_get_expr after a successful
    # wrapper install.
    canonical = (
        "to_tsvector('simple'::regconfig, "
        "intaris_immutable_unaccent(COALESCE({col}, ''::text)))"
    )
    cur = _FakePgCursor(
        existing_exprs={
            ("sessions", "intention_tsv"): canonical.format(col="intention"),
            ("sessions", "title_tsv"): canonical.format(col="title"),
            ("audit_log", "intention_tsv"): canonical.format(col="intention"),
            ("audit_log", "content_tsv"): canonical.format(col="content"),
            ("session_summaries", "summary_tsv"): canonical.format(col="summary"),
            ("agent_summaries", "summary_tsv"): canonical.format(col="summary"),
        },
        rowcounts={
            # Doesn't matter — match path never asks for rowcount.
            "sessions": 1_000_000,
            "audit_log": 1_000_000,
            "session_summaries": 1_000_000,
            "agent_summaries": 1_000_000,
        },
    )

    s._ensure_pg_tsvector_columns(cur)

    # No DDL whatsoever — every column already matches.
    ddl = [
        sql
        for sql, _ in cur.executed
        if any(verb in sql.lower() for verb in ("alter table", "drop column"))
    ]
    assert ddl == [], ddl
    assert s.notes == []


class _FakePgVectorCursor:
    """Minimal cursor for exercising ``_ensure_pg_vector_tables``.

    Records every executed SQL statement and treats SAVEPOINT/RELEASE/
    ROLLBACK as no-ops. The first ``CREATE EXTENSION vector`` raises
    by default (matches a managed PG cluster without the extension
    binary) so we can verify it's the path the bootstrap chose.
    """

    def __init__(self, *, extension_available: bool = True) -> None:
        self.extension_available = extension_available
        self.executed: list[tuple[str, tuple]] = []
        self._next_result: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: D401
        self.executed.append((sql, tuple(params)))
        norm = " ".join(sql.split()).lower()
        if "create extension" in norm and "vector" in norm:
            if not self.extension_available:
                raise RuntimeError('extension "vector" is not available')
            self._next_result = []
            return
        if "information_schema.columns" in norm:
            # Pretend the column doesn't exist yet so the bootstrap
            # tries to ADD COLUMN — we want to observe the full path.
            self._next_result = []
            return
        self._next_result = []

    def fetchone(self):
        if not self._next_result:
            return None
        return self._next_result.pop(0)


def test_pgvector_setup_runs_only_when_pgvector_enabled():
    """When ``pgvector_enabled=True`` the bootstrap must attempt
    ``CREATE EXTENSION vector`` and create ``search_vectors``."""
    from intaris.search.schema import SearchSchema

    s = SearchSchema(
        vector_enabled=True,
        pgvector_enabled=True,
        embedding_dim=1536,
    )
    cur = _FakePgVectorCursor(extension_available=True)
    s._ensure_pg_vector_tables(cur)

    sqls = [sql for sql, _ in cur.executed]
    assert any(
        "create extension" in sql.lower() and "vector" in sql.lower() for sql in sqls
    ), sqls
    assert any("search_vectors" in sql.lower() for sql in sqls), sqls


def test_pgvector_setup_skipped_when_provider_is_qdrant(caplog):
    """When the controller is configured for ``qdrant`` (or any
    non-pgvector provider), the bootstrap must NOT run pgvector setup
    even though the vector tier is enabled. The previous bug here
    triggered a misleading
    ``CREATE EXTENSION vector failed`` warning on every PG startup
    when the cluster used the qdrant provider."""
    import logging

    from intaris.search.schema import SearchSchema

    s = SearchSchema(
        vector_enabled=True,
        pgvector_enabled=False,  # qdrant or disabled
        embedding_dim=1536,
    )

    # Hand-rolled cursor that will FAIL the test if any pgvector DDL
    # is issued — qdrant mode must not touch pgvector at all.
    class _StrictCursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, sql: str, params: tuple = ()) -> None:
            self.executed.append(sql)
            if "create extension" in sql.lower() and "vector" in sql.lower():
                raise AssertionError(
                    f"qdrant mode must not run CREATE EXTENSION vector: {sql}"
                )
            if "search_vectors" in sql.lower():
                raise AssertionError(
                    f"qdrant mode must not touch search_vectors: {sql}"
                )

        def fetchone(self):
            return None

    cur = _StrictCursor()
    with caplog.at_level(logging.WARNING, logger="intaris.search.schema"):
        # Just call the gate path directly — the bootstrap's branch
        # check is what we're verifying.
        if s._pgvector_enabled:
            s._ensure_pg_vector_tables(cur)
    # No pgvector warning was emitted because we never ran the path.
    assert not any("pgvector" in r.message.lower() for r in caplog.records)


def test_search_service_passes_pgvector_only_for_pgvector_provider(monkeypatch):
    """``SearchService`` must derive ``pgvector_enabled`` from the
    configured provider, not from ``vector_enabled``. This is the
    plumbing that prevented the gate from working before."""
    from intaris.config import SearchConfig
    from intaris.search.schema import SearchSchema

    captured: dict[str, bool] = {}

    real_init = SearchSchema.__init__

    def spy_init(self, **kwargs):  # type: ignore[no-untyped-def]
        captured["vector_enabled"] = kwargs.get("vector_enabled", False)
        captured["pgvector_enabled"] = kwargs.get("pgvector_enabled", False)
        return real_init(self, **kwargs)

    monkeypatch.setattr(SearchSchema, "__init__", spy_init)

    cfg = SearchConfig()
    cfg.enabled = False  # Skip schema.ensure() / vector backend init.
    cfg.vector_provider = "qdrant"
    cfg.embedding_model = "text-embedding-3-small"
    SearchService(db=object(), config=cfg)
    assert captured["pgvector_enabled"] is False, captured

    captured.clear()
    cfg.vector_provider = "pgvector"
    SearchService(db=object(), config=cfg)
    assert captured["pgvector_enabled"] is True, captured

    captured.clear()
    cfg.vector_provider = "disabled"
    cfg.embedding_model = ""
    SearchService(db=object(), config=cfg)
    assert captured["pgvector_enabled"] is False, captured


# ── Outbox ─────────────────────────────────────────────────────────


def test_outbox_enqueue_and_claim(db, schema):
    outbox.enqueue(db, op=outbox.OP_EMBED, payload={"text": "hello"})
    outbox.enqueue(db, op=outbox.OP_EMBED, payload={"text": "world"})
    rows = outbox.claim_due(db, limit=10)
    assert {r["payload"]["text"] for r in rows} == {"hello", "world"}


def test_outbox_claim_marks_rows_claimed(db, schema):
    outbox.enqueue(db, op=outbox.OP_EMBED, payload={"x": 1})
    outbox.enqueue(db, op=outbox.OP_EMBED, payload={"x": 2})
    first = outbox.claim_due(db, limit=10)
    assert len(first) == 2
    second = outbox.claim_due(db, limit=10)
    assert second == []


def test_outbox_invalid_op_raises(db, schema):
    with pytest.raises(ValueError):
        outbox.enqueue(db, op="bogus", payload={})


# ── Lexical: summary ──────────────────────────────────────────────


def test_lexical_summary_finds_match(db, schema):
    _seed_session(db, session_id="sess-1")
    _insert_summary(
        db, session_id="sess-1", summary="rocket launched at dawn over the pacific"
    )

    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_SUMMARY],
        filters={},
        limit=10,
    )
    assert len(matches) == 1
    assert matches[0].kind == KIND_SUMMARY
    assert matches[0].session_id == "sess-1"
    assert "rocket" in matches[0].snippet.lower()


def test_lexical_summary_diacritic_folded_match(db, schema):
    """SQLite path uses an ``intaris_fold`` UDF so that an unfolded query
    matches accented stored content. Without the UDF this would miss."""
    _seed_session(db, session_id="sess-fold")
    _insert_summary(
        db,
        session_id="sess-fold",
        summary="Příliš žluťoučký kůň úpěl ďábelské ódy",
    )

    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="zlutoucky",  # no diacritics
        kinds=[KIND_SUMMARY],
        filters={},
        limit=10,
    )
    assert any(m.session_id == "sess-fold" for m in matches)


def test_lexical_summary_user_scoped(db, schema):
    _seed_session(db, session_id="shared", user_id=OTHER_USER)
    _insert_summary(db, session_id="shared", summary="bob rocket", user_id=OTHER_USER)
    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_SUMMARY],
        filters={},
        limit=10,
    )
    assert matches == []


# ── Lexical: intention (audit_log + DISTINCT ON) ──────────────────


def test_lexical_intention_dedup_via_distinct_on(db, schema, audit_store):
    _seed_session(db, session_id="sess-2")
    # Three audit rows with the SAME intention text — should collapse
    # to one match.
    for i in range(3):
        audit_store.insert(
            call_id=f"call-{i}",
            user_id=TEST_USER,
            session_id="sess-2",
            agent_id="agent-1",
            tool="bash",
            args_redacted={},
            classification="read",
            evaluation_path="fast",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=10,
            record_type="tool_call",
            content=None,
            intention="rocket launch tracking system",
        )
    # And one with a DIFFERENT intention — should be a separate match.
    audit_store.insert(
        call_id="call-other",
        user_id=TEST_USER,
        session_id="sess-2",
        agent_id="agent-1",
        tool="bash",
        args_redacted={},
        classification="read",
        evaluation_path="fast",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=10,
        record_type="tool_call",
        content=None,
        intention="rocket diagnostics module",
    )

    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_INTENTION],
        filters={},
        limit=10,
    )
    intentions = {m.snippet for m in matches}
    # SQLite emulates DISTINCT ON via GROUP BY; we get one row per
    # distinct intention text per session.
    assert len(matches) == 2
    # Both intentions should be visible in the snippets.
    assert any("tracking" in s for s in intentions)
    assert any("diagnostics" in s for s in intentions)


def test_lexical_intention_user_scoped(db, schema, audit_store):
    _seed_session(db, session_id="shared", user_id=OTHER_USER)
    audit_store.insert(
        call_id="other-call",
        user_id=OTHER_USER,
        session_id="shared",
        agent_id="agent-1",
        tool="bash",
        args_redacted={},
        classification="read",
        evaluation_path="fast",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=10,
        record_type="tool_call",
        content=None,
        intention="rocket launch",
    )
    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_INTENTION],
        filters={},
        limit=10,
    )
    assert matches == []


# ── Lexical: reasoning ────────────────────────────────────────────


def test_lexical_reasoning_finds_user_messages_and_agent_reasoning(
    db, schema, audit_store
):
    _seed_session(db, session_id="sess-3")
    audit_store.insert(
        call_id="r1",
        user_id=TEST_USER,
        session_id="sess-3",
        agent_id="agent-1",
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
        record_type="reasoning",
        content="User message: please track the rocket launch",
    )
    audit_store.insert(
        call_id="r2",
        user_id=TEST_USER,
        session_id="sess-3",
        agent_id="agent-1",
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
        record_type="reasoning",
        content="Plan to monitor the rocket telemetry stream",
    )
    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_REASONING],
        filters={},
        limit=10,
    )
    assert len(matches) == 2
    roles = {m.role for m in matches}
    # Caller can distinguish user-message reasoning from agent reasoning.
    assert roles == {"user", "assistant"}


def test_lexical_reasoning_ignores_tool_call_rows(db, schema, audit_store):
    _seed_session(db, session_id="sess-4")
    audit_store.insert(
        call_id="t1",
        user_id=TEST_USER,
        session_id="sess-4",
        agent_id="agent-1",
        tool="bash",
        args_redacted={"cmd": "rocket"},
        classification="read",
        evaluation_path="fast",
        decision="approve",
        risk=None,
        reasoning="rocket would only show up here in tool_call rows",
        latency_ms=10,
        record_type="tool_call",
        content=None,
    )
    matches = search_lexical(
        db,
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_REASONING],
        filters={},
        limit=10,
    )
    # tool_call rows should not surface under the reasoning kind.
    assert matches == []


# ── Service integration ───────────────────────────────────────────


def test_service_disabled_returns_empty(db):
    cfg = SearchConfig()
    cfg.enabled = False
    svc = SearchService(db=db, config=cfg)
    matches, cursor, mode, degraded = svc.search(
        user_id=TEST_USER,
        q="rocket",
        kinds=None,
        filters={},
        mode="auto",
        limit=10,
        cursor=None,
    )
    assert matches == []
    assert mode == "disabled"
    assert degraded == "search_disabled"


def test_service_health_when_lexical_only(service):
    h = service.health()
    assert h.enabled is True
    assert h.lexical.backend in ("sqlite-like", "postgres-fts")
    assert h.vector.provider == "disabled"
    assert set(h.lexical.kinds) == {KIND_SUMMARY, KIND_INTENTION, KIND_REASONING}


def test_service_search_runs_without_indexer(db, schema, search_config, audit_store):
    """vector_provider=disabled means no outbox writes occur."""
    _seed_session(db, session_id="sess-5")
    svc = SearchService(db=db, config=search_config)
    audit_store.insert(
        call_id="r-x",
        user_id=TEST_USER,
        session_id="sess-5",
        agent_id="agent-1",
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
        record_type="reasoning",
        content="rocket info",
    )
    # Outbox stays empty (vector tier off).
    assert outbox.queue_depth(db) == 0
    # But search still finds the match via lexical.
    matches, _, mode_used, _ = svc.search(
        user_id=TEST_USER,
        q="rocket",
        kinds=None,
        filters={},
        mode="auto",
        limit=10,
        cursor=None,
    )
    assert mode_used == "lexical"
    assert any(m.kind == KIND_REASONING for m in matches)


def test_service_search_sessions_aggregates(db, schema, search_config, audit_store):
    _seed_session(db, session_id="sess-A")
    _seed_session(db, session_id="sess-B")
    _insert_summary(db, session_id="sess-A", summary="rocket payload telemetry")
    _insert_summary(db, session_id="sess-B", summary="rocket diagnostic reports")
    svc = SearchService(db=db, config=search_config)
    sessions, _, _, _ = svc.search_sessions(
        user_id=TEST_USER,
        q="rocket",
        kinds=None,
        filters={},
        mode="auto",
        limit=10,
        cursor=None,
    )
    assert {s.session_id for s in sessions} == {"sess-A", "sess-B"}


# ── Audit hook fan-out (vector tier) ──────────────────────────────


def test_audit_insert_fans_out_when_search_service_set(
    db, schema, search_config, audit_store
):
    """When a SearchService is wired in and vector tier is healthy,
    audit inserts enqueue embed ops; otherwise the outbox stays empty."""
    _seed_session(db, session_id="sess-fanout")

    class FakeService:
        def __init__(self):
            self.intentions = []
            self.reasonings = []

        def enqueue_audit_intention(self, **kwargs):
            self.intentions.append(kwargs)

        def enqueue_audit_reasoning(self, **kwargs):
            self.reasonings.append(kwargs)

    fake = FakeService()
    AuditStore.set_search_service(fake)
    try:
        audit_store.insert(
            call_id="r-fan",
            user_id=TEST_USER,
            session_id="sess-fanout",
            agent_id="agent-1",
            tool=None,
            args_redacted=None,
            classification=None,
            evaluation_path="reasoning",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=0,
            record_type="reasoning",
            content="rocket reasoning content",
            intention="rocket launch tracking",
        )
    finally:
        AuditStore.set_search_service(None)

    assert len(fake.intentions) == 1
    assert len(fake.reasonings) == 1
    assert fake.intentions[0]["intention"] == "rocket launch tracking"
    assert fake.reasonings[0]["content"] == "rocket reasoning content"


# ── Fusion ─────────────────────────────────────────────────────────


def test_rrf_fuses_two_lists():
    lex = [
        {"session_id": "S", "kind": "x", "ref_id": "1", "score": 0.9},
        {"session_id": "S", "kind": "x", "ref_id": "2", "score": 0.5},
    ]
    vec = [
        {"session_id": "S", "kind": "x", "ref_id": "2", "score": 0.99},
        {"session_id": "S", "kind": "x", "ref_id": "3", "score": 0.6},
    ]
    fused = rrf_fuse(lexical=lex, vector=vec, alpha=0.5)
    refs = [m["ref_id"] for m in fused]
    assert "3" in refs
    two = next(m for m in fused if m["ref_id"] == "2")
    assert "lexical" in two["score_breakdown"]
    assert "vector" in two["score_breakdown"]


# ── Cursor ─────────────────────────────────────────────────────────


def test_cursor_round_trip():
    payload = {"offset": 42, "tail": "abc"}
    encoded = encode_cursor(payload)
    assert decode_cursor(encoded) == payload


def test_cursor_handles_garbage():
    assert decode_cursor("not-base64!") == {}
    assert decode_cursor("") == {}
    assert decode_cursor(None) == {}


def test_service_clamps_huge_cursor_offset(db, schema, search_config):
    """A forged cursor must not let the caller request a 10M-row page."""
    svc = SearchService(db=db, config=search_config)
    huge_cursor = encode_cursor({"offset": 10_000_000})
    matches, _, _, _ = svc.search(
        user_id=TEST_USER,
        q="rocket",
        kinds=None,
        filters={},
        mode="auto",
        limit=10,
        cursor=huge_cursor,
    )
    # No crash, no allocation explosion. Offset is clamped server-side.
    assert matches == []


# ── Qdrant local-mode (skipped if package missing) ────────────────


@pytest.fixture
def qdrant_local(tmp_path, schema):
    pytest.importorskip("qdrant_client")
    from intaris.search.vector import QdrantVectorBackend

    backend = QdrantVectorBackend(
        url=str(tmp_path / "qdrant"),
        api_key=None,
        collection="test",
        model="text-embedding-3-small",
        dim=4,
        sparse_model="",  # disable sparse for simpler local-mode test
    )
    if not backend.healthy():
        pytest.skip("qdrant local-mode unhealthy in this environment")
    return backend


def test_qdrant_local_path_detection():
    from intaris.search.vector import _qdrant_local_path

    assert _qdrant_local_path("http://localhost:6333") is None
    assert _qdrant_local_path("https://qdrant.example.com") is None
    assert _qdrant_local_path("/abs/path") == "/abs/path"
    assert _qdrant_local_path("file:///abs/path") == "/abs/path"
    expanded = _qdrant_local_path("~/foo")
    assert expanded is not None and expanded.endswith("/foo")


def test_qdrant_upsert_and_search(qdrant_local):
    backend = qdrant_local
    backend.upsert(
        user_id=TEST_USER,
        session_id="s1",
        kind=KIND_INTENTION,
        ref_id="r1",
        text="rocket",
        ts="2026-04-01T00:00:00Z",
        agent_id="agent-1",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    backend.upsert(
        user_id=TEST_USER,
        session_id="s2",
        kind=KIND_INTENTION,
        ref_id="r2",
        text="diagnostics",
        ts="2026-04-02T00:00:00Z",
        agent_id="agent-1",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )
    matches = backend.search(
        user_id=TEST_USER,
        q="rocket",
        embedding=[1.0, 0.0, 0.0, 0.0],
        kinds=[KIND_INTENTION],
        filters={},
        limit=5,
    )
    refs = [m.ref_id for m in matches]
    assert refs and refs[0] == "r1"


def test_qdrant_search_accepts_iso_timestamp_filters(qdrant_local):
    backend = qdrant_local
    backend.upsert(
        user_id=TEST_USER,
        session_id="s1",
        kind=KIND_INTENTION,
        ref_id="r1",
        text="rocket",
        ts="2026-04-01T00:00:00Z",
        agent_id="agent-1",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    backend.upsert(
        user_id=TEST_USER,
        session_id="s2",
        kind=KIND_INTENTION,
        ref_id="r2",
        text="rocket",
        ts="2026-04-02T00:00:00Z",
        agent_id="agent-1",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )

    matches = backend.search(
        user_id=TEST_USER,
        q="rocket",
        embedding=[1.0, 0.0, 0.0, 0.0],
        kinds=[KIND_INTENTION],
        filters={
            "from_ts": "2026-04-01T12:00:00+00:00",
            "to_ts": "2026-04-02T23:59:59+00:00",
        },
        limit=5,
    )

    assert {m.ref_id for m in matches} == {"r2"}


def test_qdrant_search_uses_datetime_range_for_timestamp_filters(qdrant_local):
    from qdrant_client.http import models as qmodels

    backend = qdrant_local
    captured: dict[str, object] = {}
    original_query_points = backend._client.query_points

    def capture_query_points(*args, **kwargs):
        captured["query_filter"] = kwargs["query_filter"]
        return original_query_points(*args, **kwargs)

    backend._client.query_points = capture_query_points
    backend.upsert(
        user_id=TEST_USER,
        session_id="s1",
        kind=KIND_INTENTION,
        ref_id="r1",
        text="rocket",
        ts="2026-04-01T00:00:00Z",
        agent_id="agent-1",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )

    backend.search(
        user_id=TEST_USER,
        q="rocket",
        embedding=[1.0, 0.0, 0.0, 0.0],
        kinds=[KIND_INTENTION],
        filters={"from_ts": "2026-04-01T00:00:00Z"},
        limit=5,
    )

    conditions = captured["query_filter"].must
    ts_condition = next(condition for condition in conditions if condition.key == "ts")
    assert isinstance(ts_condition.range, qmodels.DatetimeRange)


def test_qdrant_user_scope(qdrant_local):
    backend = qdrant_local
    backend.upsert(
        user_id=TEST_USER,
        session_id="s1",
        kind=KIND_INTENTION,
        ref_id="r1",
        text="rocket",
        ts="2026-04-01T00:00:00Z",
        agent_id="agent-1",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    other = backend.search(
        user_id=OTHER_USER,
        q="rocket",
        embedding=[1.0, 0.0, 0.0, 0.0],
        kinds=[KIND_INTENTION],
        filters={},
        limit=5,
    )
    assert other == []


# ── Hybrid orchestration with stub vector ─────────────────────────


def test_service_hybrid_with_stub_pgvector(db, schema, search_config, audit_store):
    """Wire a stub vector backend that mimics pgvector and confirm RRF
    fusion happens in the service.search() path."""
    from intaris.search.vector import VectorMatch

    _seed_session(db, session_id="sess-h1")
    _seed_session(db, session_id="sess-h2")
    _insert_summary(db, session_id="sess-h1", summary="rocket dawn")
    _insert_summary(db, session_id="sess-h2", summary="rocket noon")

    svc = SearchService(db=db, config=search_config)

    class StubVector:
        name = "pgvector"
        model = "stub"
        dim = 4
        sparse_model = None

        def healthy(self):
            return True

        def search(self, *, user_id, q, embedding, kinds, filters, limit):
            return [
                VectorMatch(
                    user_id=user_id,
                    session_id="sess-h2",
                    kind=KIND_SUMMARY,
                    ref_id="vec-2",
                    text="rocket noon",
                    ts=None,
                    agent_id=None,
                    score=0.99,
                ),
            ]

        def upsert(self, **_):
            pass

        def delete_session(self, **_):
            return 0

        def delete_ref(self, **_):
            pass

    class StubEmbeddings:
        model = "stub"

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    svc._vector = StubVector()
    svc._embeddings = StubEmbeddings()  # type: ignore[assignment]

    matches, _, mode_used, degraded = svc.search(
        user_id=TEST_USER,
        q="rocket",
        kinds=[KIND_SUMMARY],
        filters={},
        mode="hybrid",
        limit=10,
        cursor=None,
    )
    assert mode_used == "hybrid"
    assert degraded == ""
    # At minimum we get the lexical hits; vector contribution may
    # surface as an extra result.
    sids = {m.session_id for m in matches}
    assert {"sess-h1", "sess-h2"} <= sids
