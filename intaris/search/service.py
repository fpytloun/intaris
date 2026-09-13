"""Top-level search service.

Holds the schema bootstrap result, the optional vector backend, the
indexer worker (when vector tier is on), and the query entry points.
Constructed once at startup and attached to ``app.state.search_service``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from intaris.config import SearchConfig
from intaris.search import outbox
from intaris.search.cursor import decode_cursor, encode_cursor
from intaris.search.embeddings import EmbeddingClient, EmbeddingError
from intaris.search.fusion import rrf_fuse
from intaris.search.lexical import search_lexical
from intaris.search.schema import LEXICAL_SCHEMA_VERSION, SearchSchema
from intaris.search.state import (
    SearchStateRow,
    load_state,
    mark_indexed,
    needs_backfill,
    save_resolved_config,
    start_backfill,
    update_backfill_progress,
)
from intaris.search.types import (
    DEFAULT_KINDS,
    KIND_INTENTION,
    KIND_REASONING,
    KIND_SUMMARY,
    SEARCH_KINDS,
    SearchBackends,
    SearchHealth,
    SearchLexicalCapabilities,
    SearchMatch,
    SearchSessionMatch,
    SearchVectorState,
)
from intaris.search.vector import (
    DisabledVectorBackend,
    VectorBackend,
    VectorMatch,
    build_vector_backend,
)

logger = logging.getLogger(__name__)

# Hard cap on cursor-decoded offsets — defends against a forged cursor
# requesting a multi-million-row page that would explode the underlying
# query's ``LIMIT limit + offset + 1`` expansion.
_MAX_CURSOR_OFFSET = 10_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SearchService:
    """Coordinator: schema bootstrap + vector tier + query orchestrator."""

    def __init__(self, *, db: Any, config: SearchConfig) -> None:
        self._db = db
        self._config = config
        self._enabled = config.enabled
        self._schema = SearchSchema(
            vector_enabled=config.vector_enabled(),
            pgvector_enabled=(
                config.vector_enabled() and config.vector_provider == "pgvector"
            ),
            embedding_dim=config.embedding_dim,
        )
        self._vector: VectorBackend = DisabledVectorBackend()
        self._embeddings: EmbeddingClient | None = None
        self._indexer_task: asyncio.Task | None = None
        self._indexer_stop: asyncio.Event | None = None
        self._backfill_task: asyncio.Task | None = None
        self._backfill_lock = threading.Lock()
        self._needs_backfill = False

        if not self._enabled:
            logger.info("Search disabled (INTARIS_SEARCH_ENABLED=false)")
            return

        try:
            self._initialize_backends(db, config)
        except BaseException:
            self._vector.close()
            raise

    def _initialize_backends(self, db: Any, config: SearchConfig) -> None:
        # Schema bootstrap is synchronous and idempotent.
        self._schema.ensure(db)

        # Vector backend (only meaningful when configured).
        self._vector = build_vector_backend(db=db, config=config)
        if config.vector_enabled() and self._vector.healthy():
            try:
                self._embeddings = EmbeddingClient(
                    model=config.embedding_model,
                    base_url=config.resolve_embedding_base_url(),
                    api_key=config.resolve_embedding_api_key(),
                    timeout_ms=config.embedding_timeout_ms,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Search: embedding client init failed (%s); vector tier dormant",
                    exc,
                )
                self._embeddings = None
                self._vector.close()
                self._vector = DisabledVectorBackend()

        # Persist resolved config and detect drift.
        prev = load_state(db)
        new = SearchStateRow(
            lexical_schema_version=LEXICAL_SCHEMA_VERSION,
            vector_provider=self._vector.name,
            vector_model=(self._vector.model or None),
            vector_dim=(self._vector.dim or None),
            sparse_model=(
                self._vector.sparse_model
                if hasattr(self._vector, "sparse_model")
                else None
            ),
        )
        save_resolved_config(
            db,
            lexical_schema_version=new.lexical_schema_version,
            vector_provider=new.vector_provider,
            vector_model=new.vector_model,
            vector_dim=new.vector_dim,
            sparse_model=new.sparse_model,
            notes=list(self._schema.notes),
        )
        self._needs_backfill = self._vector.healthy() and needs_backfill(prev, new)

    # ── lifecycle ─────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def lexical_backend(self) -> str:
        return self._schema.lexical_backend if self._enabled else "disabled"

    @property
    def vector_backend_name(self) -> str:
        return self._vector.name if self._enabled else "disabled"

    async def start(self) -> None:
        if not self._enabled:
            return
        if not self._vector.healthy():
            return  # No indexer needed for lexical-only mode.
        loop = asyncio.get_running_loop()
        self._indexer_stop = asyncio.Event()
        self._indexer_task = loop.create_task(
            self._indexer_loop(), name="intaris-search-indexer"
        )

        if self._needs_backfill:
            try:
                await self.trigger_reindex(reason="auto_drift")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Search: auto backfill enqueue failed: %s", exc)

    async def stop(self) -> None:
        if self._indexer_stop is not None:
            self._indexer_stop.set()
        if self._indexer_task is not None:
            try:
                await asyncio.wait_for(self._indexer_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self._backfill_task is not None:
            try:
                await asyncio.wait_for(self._backfill_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        # Close vector backend client (Qdrant local-mode holds an
        # embedded SQLite handle that needs an explicit close).
        try:
            close = getattr(self._vector, "close", None)
            if callable(close):
                close()
        except Exception:  # noqa: BLE001
            pass

    # ── enqueue helpers (called from canonical write paths) ──

    def enqueue_audit_intention(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        ref_id: str,
        intention: str,
        ts: str | None,
    ) -> None:
        if not self._enabled or not self._vector.healthy():
            return
        if not intention or not intention.strip():
            return
        self._enqueue_embed(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=KIND_INTENTION,
            ref_id=ref_id,
            text=intention,
            ts=ts,
        )

    def enqueue_audit_reasoning(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        ref_id: str,
        content: str,
        ts: str | None,
    ) -> None:
        if not self._enabled or not self._vector.healthy():
            return
        if not content or not content.strip():
            return
        self._enqueue_embed(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=KIND_REASONING,
            ref_id=ref_id,
            text=content,
            ts=ts,
        )

    def enqueue_summary(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        ref_id: str,
        summary: str,
        ts: str | None,
    ) -> None:
        if not self._enabled or not self._vector.healthy():
            return
        if not summary or not summary.strip():
            return
        self._enqueue_embed(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=KIND_SUMMARY,
            ref_id=ref_id,
            text=summary,
            ts=ts,
        )

    def enqueue_session_delete(self, *, user_id: str, session_id: str) -> None:
        if not self._enabled or not self._vector.healthy():
            return
        outbox.enqueue(
            self._db,
            op=outbox.OP_DELETE_SESSION,
            payload={"user_id": user_id, "session_id": session_id},
        )

    def _enqueue_embed(
        self,
        *,
        user_id: str,
        session_id: str,
        agent_id: str | None,
        kind: str,
        ref_id: str,
        text: str,
        ts: str | None,
    ) -> None:
        max_bytes = self._config.max_text_bytes
        truncated = text
        if len(text.encode("utf-8", errors="ignore")) > max_bytes:
            truncated = text.encode("utf-8", errors="ignore")[:max_bytes].decode(
                "utf-8", errors="ignore"
            )
        outbox.enqueue(
            self._db,
            op=outbox.OP_EMBED,
            payload={
                "user_id": user_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "kind": kind,
                "ref_id": ref_id,
                "text": truncated,
                "ts": ts,
            },
        )

    # ── indexer loop ──────────────────────────────────────────────

    async def _indexer_loop(self) -> None:
        assert self._indexer_stop is not None
        while not self._indexer_stop.is_set():
            try:
                processed = await asyncio.to_thread(self._drain_outbox_once)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Search indexer iteration failed: %s", exc)
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._indexer_stop.wait(),
                        timeout=self._config.indexer_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)

    def _drain_outbox_once(self) -> int:
        rows = outbox.claim_due(self._db, limit=64)
        if not rows:
            return 0
        successes: list[int] = []
        embed_rows: list[dict[str, Any]] = []
        for row in rows:
            try:
                if row["op"] == outbox.OP_EMBED:
                    embed_rows.append(row)
                elif row["op"] == outbox.OP_DELETE_SESSION:
                    payload = row["payload"] or {}
                    self._delete_session(
                        user_id=payload.get("user_id") or "",
                        session_id=payload.get("session_id") or "",
                    )
                    successes.append(row["id"])
                elif row["op"] == outbox.OP_DELETE_REF:
                    payload = row["payload"] or {}
                    self._vector.delete_ref(
                        user_id=payload.get("user_id") or "",
                        session_id=payload.get("session_id") or "",
                        kind=payload.get("kind") or "",
                        ref_id=payload.get("ref_id") or "",
                    )
                    successes.append(row["id"])
                else:
                    successes.append(row["id"])
            except Exception as exc:  # noqa: BLE001
                outbox.mark_failed(
                    self._db,
                    row_id=row["id"],
                    attempts=row["attempts"] + 1,
                    error=str(exc)[:1024],
                )
        if embed_rows:
            self._dispatch_embed_batch(embed_rows, successes)
        if successes:
            outbox.mark_done(self._db, successes)
            mark_indexed(self._db)
        return len(rows)

    def _dispatch_embed_batch(
        self, rows: list[dict[str, Any]], successes: list[int]
    ) -> None:
        if self._embeddings is None or not self._vector.healthy():
            outbox.mark_done(self._db, [r["id"] for r in rows])
            return

        batch_size = max(1, self._config.embedding_batch_size)
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start : batch_start + batch_size]
            texts = [str(r["payload"].get("text") or "") for r in batch]
            try:
                vectors = self._embeddings.embed(texts)
            except EmbeddingError as exc:
                # Log only the exception class — provider error
                # messages can echo input fragments.
                logger.warning(
                    "Search: embedding batch failed (%s)", type(exc).__name__
                )
                for r in batch:
                    outbox.mark_failed(
                        self._db,
                        row_id=r["id"],
                        attempts=r["attempts"] + 1,
                        error=str(exc)[:1024],
                    )
                continue

            for r, vector in zip(batch, vectors, strict=False):
                payload = r["payload"] or {}
                try:
                    self._vector.upsert(
                        user_id=payload.get("user_id") or "",
                        session_id=payload.get("session_id") or "",
                        kind=payload.get("kind") or "",
                        ref_id=payload.get("ref_id") or "",
                        text=str(payload.get("text") or ""),
                        ts=payload.get("ts"),
                        agent_id=payload.get("agent_id"),
                        embedding=vector,
                    )
                    successes.append(r["id"])
                except Exception as exc:  # noqa: BLE001
                    outbox.mark_failed(
                        self._db,
                        row_id=r["id"],
                        attempts=r["attempts"] + 1,
                        error=str(exc)[:1024],
                    )

    def _delete_session(self, *, user_id: str, session_id: str) -> None:
        if not user_id or not session_id:
            return
        try:
            self._vector.delete_session(user_id=user_id, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Search: vector delete_session failed (%s)", exc)

    # ── query ────────────────────────────────────────────────────

    def search(
        self,
        *,
        user_id: str,
        q: str,
        kinds: Iterable[str] | None,
        filters: dict[str, Any],
        mode: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[SearchMatch], str | None, str, str]:
        """Run a search and return (matches, next_cursor, mode_used, degraded)."""
        if not self._enabled:
            return [], None, "disabled", "search_disabled"

        decoded = decode_cursor(cursor)
        # Clamp offset so a crafted cursor cannot ask for huge pages.
        # The query layer below expands ``page_limit = limit + offset + 1``
        # so unbounded offsets would let a malicious cursor allocate
        # arbitrarily large result sets server-side.
        try:
            offset = int(decoded.get("offset", 0)) if cursor else 0
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, min(offset, _MAX_CURSOR_OFFSET))
        kind_set: set[str] = (
            {k for k in kinds if k in SEARCH_KINDS} if kinds else set(DEFAULT_KINDS)
        )
        if not kind_set:
            return [], None, "lexical", ""

        effective, degraded = self._resolve_mode(mode)
        page_limit = limit + offset + 1

        # Qdrant mode: server-side hybrid only, no PG lexical step.
        if effective in ("vector", "hybrid") and self._vector.name == "qdrant":
            try:
                vec_matches = self._run_vector_query(
                    user_id=user_id,
                    q=q,
                    kinds=kind_set,
                    filters=filters,
                    limit=page_limit,
                )
            except EmbeddingError:
                degraded = "vector_unavailable"
                vec_matches = []
                effective = "lexical"
            else:
                rows = [self._vector_match_to_row(m) for m in vec_matches]
                page = rows[offset : offset + limit]
                next_cursor = (
                    encode_cursor({"offset": offset + limit})
                    if len(rows) > offset + limit
                    else None
                )
                matches = [self._row_to_match(r) for r in page]
                return matches, next_cursor, "vector", degraded

        # pgvector / disabled path: PG lexical optionally fused with pgvector.
        lex_matches = search_lexical(
            self._db,
            user_id=user_id,
            q=q,
            kinds=kind_set,
            filters=filters,
            limit=page_limit,
            has_unaccent=self._schema.has_unaccent,
            has_pg_trgm=self._schema.has_pg_trgm,
        )
        lex_rows = [self._lex_match_to_row(m) for m in lex_matches]

        vec_rows: list[dict[str, Any]] = []
        if effective in ("vector", "hybrid") and self._vector.name == "pgvector":
            try:
                vec_matches = self._run_vector_query(
                    user_id=user_id,
                    q=q,
                    kinds=kind_set,
                    filters=filters,
                    limit=page_limit,
                )
                vec_rows = [self._vector_match_to_row(m) for m in vec_matches]
            except EmbeddingError:
                degraded = "vector_unavailable"
                effective = "lexical"

        if effective == "hybrid" and lex_rows and vec_rows:
            fused = rrf_fuse(
                lexical=lex_rows,
                vector=vec_rows,
                alpha=self._config.hybrid_alpha,
            )
        elif effective == "vector":
            fused = vec_rows
        else:
            fused = lex_rows
            if effective == "hybrid" and not vec_rows:
                effective = "lexical"

        page = fused[offset : offset + limit]
        next_cursor = (
            encode_cursor({"offset": offset + limit})
            if len(fused) > offset + limit
            else None
        )
        return [self._row_to_match(r) for r in page], next_cursor, effective, degraded

    def _run_vector_query(
        self,
        *,
        user_id: str,
        q: str,
        kinds: set[str],
        filters: dict[str, Any],
        limit: int,
    ) -> list[VectorMatch]:
        if self._embeddings is None or not self._vector.healthy():
            return []
        embedding = self._embeddings.embed([q])[0]
        return self._vector.search(
            user_id=user_id,
            q=q,
            embedding=embedding,
            kinds=kinds,
            filters=filters,
            limit=limit,
        )

    def _resolve_mode(self, mode: str) -> tuple[str, str]:
        requested = mode or "auto"
        vector_ready = self._vector.healthy() and self._embeddings is not None
        if requested == "auto":
            return ("hybrid" if vector_ready else "lexical"), ""
        if requested == "vector":
            return ("vector" if vector_ready else "lexical"), (
                "" if vector_ready else "vector_unavailable"
            )
        if requested == "hybrid":
            return ("hybrid" if vector_ready else "lexical"), (
                "" if vector_ready else "vector_unavailable"
            )
        return "lexical", ""

    @staticmethod
    def _lex_match_to_row(m: SearchMatch) -> dict[str, Any]:
        return {
            "session_id": m.session_id,
            "kind": m.kind,
            "ref_id": m.ref_id,
            "role": m.role,
            "ts": m.ts,
            "agent_id": m.agent_id,
            "snippet": m.snippet,
            "score": m.score,
            "score_breakdown": dict(m.score_breakdown),
        }

    @staticmethod
    def _vector_match_to_row(m: VectorMatch) -> dict[str, Any]:
        from intaris.search.lexical import _heuristic_snippet

        return {
            "session_id": m.session_id,
            "kind": m.kind,
            "ref_id": m.ref_id,
            "role": None,
            "ts": m.ts,
            "agent_id": m.agent_id,
            "snippet": _heuristic_snippet(m.text, ""),
            "score": m.score,
            "score_breakdown": {"vector": m.score},
        }

    def _row_to_match(self, row: dict[str, Any]) -> SearchMatch:
        return SearchMatch(
            session_id=row.get("session_id") or "",
            kind=row.get("kind") or "",
            ref_id=row.get("ref_id"),
            role=row.get("role"),
            ts=row.get("ts"),
            snippet=row.get("snippet") or "",
            score=float(row.get("score") or 0.0),
            score_breakdown=row.get("score_breakdown") or {},
            agent_id=row.get("agent_id"),
        )

    # ── search_sessions (aggregation) ────────────────────────────

    def search_sessions(
        self,
        *,
        user_id: str,
        q: str,
        kinds: Iterable[str] | None,
        filters: dict[str, Any],
        mode: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[SearchSessionMatch], str | None, str, str]:
        decoded = decode_cursor(cursor) if cursor else {}
        seen_sessions: set[str] = set(decoded.get("seen") or [])
        flat_cursor: str | None = decoded.get("flat_cursor")

        per_session: dict[str, dict[str, Any]] = {}
        effective = "lexical"
        degraded = ""
        match_limit = max(limit * 5, 50)
        guard = 5
        while len(per_session) < limit and guard > 0:
            guard -= 1
            matches, next_flat, effective, degraded = self.search(
                user_id=user_id,
                q=q,
                kinds=kinds,
                filters=filters,
                mode=mode,
                limit=match_limit,
                cursor=flat_cursor,
            )
            if not matches:
                flat_cursor = None
                break
            for m in matches:
                if m.session_id in seen_sessions:
                    continue
                existing = per_session.get(m.session_id)
                if existing is None:
                    per_session[m.session_id] = {"top": m, "count": 1}
                else:
                    existing["count"] += 1
                    if m.score > existing["top"].score:
                        existing["top"] = m
            flat_cursor = next_flat
            if flat_cursor is None:
                break

        if not per_session:
            return [], None, effective, degraded

        session_ids = list(per_session.keys())[:limit]
        meta = self._load_session_meta(user_id=user_id, session_ids=session_ids)

        out: list[SearchSessionMatch] = []
        for sid in session_ids:
            info = per_session[sid]
            row = meta.get(sid, {})
            out.append(
                SearchSessionMatch(
                    session_id=sid,
                    agent_id=row.get("agent_id"),
                    title=row.get("title"),
                    intention=row.get("intention"),
                    last_activity_at=row.get("last_activity_at"),
                    match_count=int(info["count"]),
                    top_match=info["top"],
                )
            )
        new_seen = list(seen_sessions | set(session_ids))
        next_cursor: str | None = None
        if flat_cursor is not None:
            next_cursor = encode_cursor({"seen": new_seen, "flat_cursor": flat_cursor})
        return out, next_cursor, effective, degraded

    def _load_session_meta(
        self, *, user_id: str, session_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not session_ids:
            return {}
        placeholders = ",".join(["?"] * len(session_ids))
        params: list[Any] = [user_id, *session_ids]
        rows: dict[str, dict[str, Any]] = {}
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT session_id, agent_id, title, intention, last_activity_at "
                "FROM sessions "
                f"WHERE user_id = ? AND session_id IN ({placeholders})",
                tuple(params),
            )
            for row in cur.fetchall():
                rows[str(row[0])] = {
                    "agent_id": (str(row[1]) if row[1] else None),
                    "title": (str(row[2]) if row[2] else None),
                    "intention": (str(row[3]) if row[3] else None),
                    "last_activity_at": (str(row[4]) if row[4] else None),
                }
        return rows

    # ── health / config / reindex ────────────────────────────────

    def health(self) -> SearchHealth:
        if not self._enabled:
            return SearchHealth(
                enabled=False,
                lexical=SearchLexicalCapabilities(backend="disabled"),
                vector=SearchVectorState(provider="disabled"),
            )
        state = load_state(self._db)
        depth = outbox.queue_depth(self._db)
        return SearchHealth(
            enabled=True,
            lexical=SearchLexicalCapabilities(
                backend=self._schema.lexical_backend,
                unaccent=self._schema.has_unaccent,
                pg_trgm=self._schema.has_pg_trgm,
                kinds=[KIND_SUMMARY, KIND_INTENTION, KIND_REASONING],
            ),
            vector=SearchVectorState(
                provider=self._vector.name,
                model=(self._vector.model or None),
                dim=(self._vector.dim or None),
                sparse_model=getattr(self._vector, "sparse_model", None),
                queue_depth=depth,
                last_index_at=state.last_index_at,
                backfill_status=state.backfill_status,
                backfill_total=state.backfill_total,
                backfill_processed=state.backfill_processed,
                backfill_job_id=state.backfill_job_id,
            ),
            notes=state.notes,
        )

    async def trigger_reindex(self, *, reason: str = "manual") -> str:
        if not self._enabled:
            raise RuntimeError("search disabled")
        if not self._vector.healthy():
            raise RuntimeError("vector tier disabled")

        with self._backfill_lock:
            existing = load_state(self._db)
            if existing.backfill_status == "running" and existing.backfill_job_id:
                return existing.backfill_job_id
            total = await asyncio.to_thread(self._count_indexable_units)
            job_id = start_backfill(self._db, total=total)

        loop = asyncio.get_running_loop()
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = loop.create_task(
                self._run_backfill(job_id), name="intaris-search-backfill"
            )
        logger.info(
            "Search: backfill %s scheduled (reason=%s, total=%d)",
            job_id,
            reason,
            total,
        )
        return job_id

    def _count_indexable_units(self) -> int:
        # Cheap approximate count: sessions × ~3 (intention + summaries).
        # Used only for progress display.
        with self._db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sessions")
            row = cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def _run_backfill(self, job_id: str) -> None:
        try:
            update_backfill_progress(self._db, processed=0, status="running")
            batch = max(1, self._config.backfill_batch_size)
            processed = await asyncio.to_thread(self._backfill_iteration, batch)
            update_backfill_progress(self._db, processed=processed, status="done")
            logger.info(
                "Search: backfill %s complete (processed=%d)", job_id, processed
            )
        except Exception as exc:  # noqa: BLE001
            update_backfill_progress(
                self._db, processed=0, status="failed", error=str(exc)[:512]
            )
            logger.exception("Search: backfill %s failed", job_id)

    def _backfill_iteration(self, batch: int) -> int:
        """Walk the canonical tables and enqueue embed ops for each row.

        Lexical search is automatic (generated columns), so backfill
        only needs to populate the vector tier.
        """
        if not self._vector.healthy():
            return 0

        offset = 0
        processed = 0

        # 1. Distinct intentions per session (audit_log).
        while True:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT MIN(id) AS id, user_id, session_id, agent_id, "
                    "       intention, MIN(timestamp) AS ts "
                    "FROM audit_log "
                    "WHERE intention IS NOT NULL "
                    "GROUP BY user_id, session_id, agent_id, intention "
                    "ORDER BY ts DESC "
                    "LIMIT ? OFFSET ?",
                    (batch, offset),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for row in rows:
                self.enqueue_audit_intention(
                    user_id=str(row[1] or ""),
                    session_id=str(row[2] or ""),
                    agent_id=(str(row[3]) if row[3] else None),
                    ref_id=str(row[0]),
                    intention=str(row[4] or ""),
                    ts=str(row[5]) if row[5] else None,
                )
            processed += len(rows)
            update_backfill_progress(self._db, processed=processed)
            offset += batch

        # 2. Reasoning rows.
        offset = 0
        while True:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, session_id, agent_id, content, timestamp "
                    "FROM audit_log "
                    "WHERE record_type IN ('reasoning','checkpoint') "
                    "  AND content IS NOT NULL "
                    "ORDER BY timestamp DESC "
                    "LIMIT ? OFFSET ?",
                    (batch, offset),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for row in rows:
                self.enqueue_audit_reasoning(
                    user_id=str(row[1] or ""),
                    session_id=str(row[2] or ""),
                    agent_id=(str(row[3]) if row[3] else None),
                    ref_id=str(row[0]),
                    content=str(row[4] or ""),
                    ts=str(row[5]) if row[5] else None,
                )
            processed += len(rows)
            update_backfill_progress(self._db, processed=processed)
            offset += batch

        # 3. Session summaries.
        offset = 0
        while True:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, session_id, summary, window_end "
                    "FROM session_summaries "
                    "ORDER BY window_end DESC "
                    "LIMIT ? OFFSET ?",
                    (batch, offset),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for row in rows:
                self.enqueue_summary(
                    user_id=str(row[1] or ""),
                    session_id=str(row[2] or ""),
                    agent_id=None,
                    ref_id=str(row[0]),
                    summary=str(row[3] or ""),
                    ts=str(row[4]) if row[4] else None,
                )
            processed += len(rows)
            update_backfill_progress(self._db, processed=processed)
            offset += batch

        # 4. Agent summaries.
        offset = 0
        while True:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, session_id, summary, created_at "
                    "FROM agent_summaries "
                    "ORDER BY created_at DESC "
                    "LIMIT ? OFFSET ?",
                    (batch, offset),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for row in rows:
                self.enqueue_summary(
                    user_id=str(row[1] or ""),
                    session_id=str(row[2] or ""),
                    agent_id=None,
                    ref_id=f"agent:{row[0]}",
                    summary=str(row[3] or ""),
                    ts=str(row[4]) if row[4] else None,
                )
            processed += len(rows)
            update_backfill_progress(self._db, processed=processed)
            offset += batch

        return processed

    def make_backends(self) -> SearchBackends:
        return SearchBackends(
            lexical=self._schema.lexical_backend,
            vector=self._vector.name,
            mode_used="auto",
        )


__all__ = ["SearchService"]
