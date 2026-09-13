"""Optional vector tier backends.

Two providers, both writing the same three search kinds (``summary``,
``intention``, ``reasoning``):

- ``pgvector``: dense embeddings only, in a Postgres ``search_vectors``
  table with HNSW cosine. The query layer RRF-fuses with PG lexical.
- ``qdrant``: native dense + sparse hybrid in a single collection.
  Sparse vectors come from FastEmbed's local ``Qdrant/bm25`` model
  (no inference call). Server-side RRF fusion via the Query API. PG
  lexical is bypassed in this mode.

When ``qdrant_url`` looks like a filesystem path (``/abs/path`` or
``file:///...``) the client switches to local-mode (no service
required). This is the recommended single-user / quickstart path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _parse_qdrant_datetime(value: Any) -> datetime | None:
    """Convert ISO timestamps to datetimes for Qdrant datetime filters."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        logger.warning("qdrant: ignoring invalid timestamp filter %r", value)
        return None


@dataclass
class VectorMatch:
    user_id: str
    session_id: str
    kind: str
    ref_id: str
    text: str
    ts: str | None
    agent_id: str | None
    score: float


class VectorBackend(Protocol):
    name: str
    dim: int
    model: str
    sparse_model: str | None

    def healthy(self) -> bool: ...

    def upsert(
        self,
        *,
        user_id: str,
        session_id: str,
        kind: str,
        ref_id: str,
        text: str,
        ts: str | None,
        agent_id: str | None,
        embedding: list[float],
    ) -> None: ...

    def search(
        self,
        *,
        user_id: str,
        q: str,
        embedding: list[float],
        kinds: Iterable[str],
        filters: dict[str, Any],
        limit: int,
    ) -> list[VectorMatch]: ...

    def delete_session(self, *, user_id: str, session_id: str) -> int: ...

    def delete_ref(
        self, *, user_id: str, session_id: str, kind: str, ref_id: str
    ) -> None: ...

    def close(self) -> None: ...


# ── Disabled ───────────────────────────────────────────────────────


class DisabledVectorBackend:
    name = "disabled"

    def __init__(self) -> None:
        self.dim = 0
        self.model = ""
        self.sparse_model: str | None = None

    def healthy(self) -> bool:
        return False

    def upsert(self, **_: Any) -> None:
        return None

    def search(self, **_: Any) -> list[VectorMatch]:
        return []

    def delete_session(self, **_: Any) -> int:
        return 0

    def delete_ref(self, **_: Any) -> None:
        return None

    def close(self) -> None:
        return None


# ── pgvector ───────────────────────────────────────────────────────


class PgVectorBackend:
    name = "pgvector"

    def __init__(self, *, db: Any, model: str, dim: int) -> None:
        if db.backend != "postgresql":
            raise RuntimeError("pgvector requires PostgreSQL backend")
        self._db = db
        self.model = model
        self.dim = dim
        self.sparse_model: str | None = None
        self._healthy = self._verify()

    def _verify(self) -> bool:
        # ``search_vectors`` and the embedding column are created by
        # SearchSchema. Verify the column dim matches; if it drifts,
        # log and let the orchestrator handle the auto-backfill.
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = 'search_vectors'::regclass "
                    "  AND attname = 'embedding' AND NOT attisdropped"
                )
                row = cur.fetchone()
            if row is None:
                logger.warning("pgvector: search_vectors.embedding column missing")
                return False
            existing_dim = int(row[0]) if row[0] is not None else -1
            if existing_dim > 0 and existing_dim != self.dim:
                logger.warning(
                    "pgvector: embedding dim drift (stored=%d, configured=%d); "
                    "drop and recreate the column to converge",
                    existing_dim,
                    self.dim,
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("pgvector: verify failed (%s)", exc)
            return False

    def healthy(self) -> bool:
        return self._healthy

    def upsert(
        self,
        *,
        user_id: str,
        session_id: str,
        kind: str,
        ref_id: str,
        text: str,
        ts: str | None,
        agent_id: str | None,
        embedding: list[float],
    ) -> None:
        if not self._healthy:
            return
        if len(embedding) != self.dim:
            raise ValueError(
                f"pgvector: embedding length {len(embedding)} does not "
                f"match configured dim {self.dim}"
            )
        vector_literal = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
        with self._db.cursor() as cur:
            cur.execute(
                "INSERT INTO search_vectors "
                "  (user_id, session_id, kind, ref_id, text, ts, agent_id, "
                "   embedding, model, dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?::vector, ?, ?) "
                "ON CONFLICT (user_id, session_id, kind, ref_id) DO UPDATE SET "
                "  text = EXCLUDED.text, "
                "  ts = EXCLUDED.ts, "
                "  agent_id = EXCLUDED.agent_id, "
                "  embedding = EXCLUDED.embedding, "
                "  model = EXCLUDED.model, "
                "  dim = EXCLUDED.dim",
                (
                    user_id,
                    session_id,
                    kind,
                    ref_id,
                    text,
                    ts,
                    agent_id,
                    vector_literal,
                    self.model,
                    self.dim,
                ),
            )

    def search(
        self,
        *,
        user_id: str,
        q: str,
        embedding: list[float],
        kinds: Iterable[str],
        filters: dict[str, Any],
        limit: int,
    ) -> list[VectorMatch]:
        if not self._healthy or len(embedding) != self.dim:
            return []
        kind_list = list(kinds)
        if not kind_list:
            return []
        where = ["user_id = ?"]
        params: list[Any] = [user_id]
        placeholders = ",".join(["?"] * len(kind_list))
        where.append(f"kind IN ({placeholders})")
        params.extend(kind_list)

        agent_id = filters.get("agent_id")
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        sids = filters.get("session_ids") or (
            [filters["session_id"]] if filters.get("session_id") else None
        )
        if sids:
            ph = ",".join(["?"] * len(sids))
            where.append(f"session_id IN ({ph})")
            params.extend(sids)
        if filters.get("from_ts"):
            where.append("ts >= ?")
            params.append(filters["from_ts"])
        if filters.get("to_ts"):
            where.append("ts <= ?")
            params.append(filters["to_ts"])

        vector_literal = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
        sql = (
            "SELECT user_id, session_id, kind, ref_id, text, ts, agent_id, "
            "       1 - (embedding <=> ?::vector) AS score "
            "FROM search_vectors "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY embedding <=> ?::vector ASC LIMIT ?"
        )
        qparams: list[Any] = [vector_literal, *params, vector_literal, limit]
        with self._db.cursor() as cur:
            cur.execute(sql, tuple(qparams))
            rows = cur.fetchall()
        return [
            VectorMatch(
                user_id=str(r[0]),
                session_id=str(r[1]),
                kind=str(r[2]),
                ref_id=str(r[3]),
                text=str(r[4] or ""),
                ts=(str(r[5]) if r[5] is not None else None),
                agent_id=(str(r[6]) if r[6] is not None else None),
                score=float(r[7] or 0.0),
            )
            for r in rows
        ]

    def delete_session(self, *, user_id: str, session_id: str) -> int:
        if not self._healthy:
            return 0
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM search_vectors WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def delete_ref(
        self, *, user_id: str, session_id: str, kind: str, ref_id: str
    ) -> None:
        if not self._healthy:
            return
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM search_vectors "
                "WHERE user_id = ? AND session_id = ? AND kind = ? "
                "  AND ref_id = ?",
                (user_id, session_id, kind, ref_id),
            )

    def close(self) -> None:
        # Shares the global DB connection pool — nothing to close here.
        return None


# ── Qdrant native dense + sparse hybrid ────────────────────────────


def _qdrant_local_path(url: str) -> str | None:
    """Return a local filesystem path if ``url`` is a path, else None."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return None
    if url.startswith("file://"):
        return urlparse(url).path or None
    expanded = os.path.expanduser(url)
    if expanded.startswith("/") or expanded.startswith("./"):
        return expanded
    if len(expanded) > 2 and expanded[1] == ":":
        return expanded
    return None


# Stable point ID per (user_id, session_id, kind, ref_id). Qdrant
# requires UUID or unsigned int IDs; we hash to a 128-bit integer
# packed as UUID string.
def _qdrant_point_id(user_id: str, session_id: str, kind: str, ref_id: str) -> str:
    import hashlib
    import uuid

    digest = hashlib.sha1(
        f"{user_id}\x00{session_id}\x00{kind}\x00{ref_id}".encode("utf-8")
    ).digest()
    return str(uuid.UUID(bytes=digest[:16]))


class QdrantVectorBackend:
    """Qdrant native hybrid (dense + sparse) backend.

    Stores both dense and sparse vectors per point. Search uses the
    Query API with a ``prefetch + Fusion(RRF)`` clause to fuse the two
    server-side. PG lexical is bypassed when this provider is in use
    because Qdrant's sparse vector covers token recall.
    """

    name = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        collection: str,
        model: str,
        dim: int,
        sparse_model: str,
    ) -> None:
        self._url = url
        self._api_key = api_key or None
        self._collection = collection
        self.model = model
        self.dim = dim
        self.sparse_model: str | None = sparse_model
        self._healthy = False
        self._client: Any = None
        self._qmodels: Any = None
        self._sparse_embedder: Any = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            from qdrant_client import QdrantClient  # type: ignore[import-not-found]
            from qdrant_client.http import (  # type: ignore[import-not-found]
                models as qmodels,
            )
        except ImportError:
            # ``qdrant-client`` is a mandatory core dependency. A
            # missing import here means the install is broken — log
            # ERROR rather than WARNING so it surfaces in alerts.
            logger.error(
                "qdrant: qdrant-client unavailable despite being a core "
                "dependency; vector tier disabled. Reinstall intaris."
            )
            return

        local_path = _qdrant_local_path(self._url)
        try:
            if local_path is not None:
                os.makedirs(local_path, exist_ok=True)
                self._client = QdrantClient(path=local_path)
                logger.info(
                    "qdrant: local-mode at %s (collection=%s)",
                    local_path,
                    self._collection,
                )
            else:
                self._client = QdrantClient(url=self._url, api_key=self._api_key)
                logger.info(
                    "qdrant: server-mode at %s (collection=%s)",
                    self._url,
                    self._collection,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant: client init failed (%s)", exc)
            self._client = None
            return

        self._qmodels = qmodels
        try:
            self._ensure_collection()
            self._init_sparse_embedder()
            self._healthy = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qdrant: collection bootstrap failed (%s); vector tier disabled",
                exc,
            )
            self._healthy = False

    def _ensure_collection(self) -> None:
        existing = self._client.get_collections().collections
        names = {c.name for c in existing}
        is_local = _qdrant_local_path(self._url) is not None

        vectors_config = {
            "dense": self._qmodels.VectorParams(
                size=self.dim, distance=self._qmodels.Distance.COSINE
            ),
        }
        sparse_vectors_config = {
            "sparse": self._qmodels.SparseVectorParams(
                modifier=self._qmodels.Modifier.IDF
            ),
        }

        if self._collection not in names:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=vectors_config,
                sparse_vectors_config=sparse_vectors_config,
            )
            if not is_local:
                for field, schema in (
                    ("user_id", self._qmodels.PayloadSchemaType.KEYWORD),
                    ("agent_id", self._qmodels.PayloadSchemaType.KEYWORD),
                    ("session_id", self._qmodels.PayloadSchemaType.KEYWORD),
                    ("kind", self._qmodels.PayloadSchemaType.KEYWORD),
                    ("ts", self._qmodels.PayloadSchemaType.DATETIME),
                ):
                    try:
                        self._client.create_payload_index(
                            collection_name=self._collection,
                            field_name=field,
                            field_schema=schema,
                        )
                    except Exception:  # noqa: BLE001
                        pass
        else:
            # Verify dimension; recreate on drift.
            try:
                info = self._client.get_collection(self._collection)
                params = info.config.params
                vec_cfg = getattr(params, "vectors", None)
                size: int | None = None
                if vec_cfg is not None:
                    if isinstance(vec_cfg, dict) and "dense" in vec_cfg:
                        size = int(getattr(vec_cfg["dense"], "size", 0)) or None
                    elif hasattr(vec_cfg, "size"):
                        size = int(vec_cfg.size)
                if size is not None and size != self.dim:
                    logger.warning(
                        "qdrant: collection %s has dim=%d but configured "
                        "dim=%d; recreating",
                        self._collection,
                        size,
                        self.dim,
                    )
                    self._client.delete_collection(self._collection)
                    self._client.create_collection(
                        collection_name=self._collection,
                        vectors_config=vectors_config,
                        sparse_vectors_config=sparse_vectors_config,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "qdrant: dimension probe skipped (%s)",
                    type(exc).__name__,
                )

    def _init_sparse_embedder(self) -> None:
        if not self.sparse_model:
            self._sparse_embedder = None
            return
        try:
            # FastEmbed sparse models — no inference call, runs locally.
            from fastembed import SparseTextEmbedding  # type: ignore[import-not-found]
        except ImportError:
            # ``fastembed`` is a mandatory core dependency. A missing
            # import here means the install is broken — log ERROR.
            logger.error(
                "qdrant: fastembed unavailable despite being a core "
                "dependency; sparse-vector recall disabled (dense-only "
                "fallback). Reinstall intaris."
            )
            self._sparse_embedder = None
            return
        try:
            self._sparse_embedder = SparseTextEmbedding(model_name=self.sparse_model)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qdrant: sparse embedder init failed (%s); falling back to dense-only",
                exc,
            )
            self._sparse_embedder = None

    def _sparse_vector(self, text: str) -> Any | None:
        if self._sparse_embedder is None or not text:
            return None
        try:
            embedding = next(self._sparse_embedder.embed([text]))
            indices = list(int(i) for i in embedding.indices)
            values = list(float(v) for v in embedding.values)
            return self._qmodels.SparseVector(indices=indices, values=values)
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant: sparse embed failed (%s)", exc)
            return None

    def healthy(self) -> bool:
        return self._healthy and self._client is not None

    def upsert(
        self,
        *,
        user_id: str,
        session_id: str,
        kind: str,
        ref_id: str,
        text: str,
        ts: str | None,
        agent_id: str | None,
        embedding: list[float],
    ) -> None:
        if not self.healthy():
            return
        if len(embedding) != self.dim:
            raise ValueError(
                f"qdrant: embedding length {len(embedding)} does not "
                f"match configured dim {self.dim}"
            )
        payload = {
            "user_id": user_id,
            "session_id": session_id,
            "kind": kind,
            "ref_id": ref_id,
            "text": text,
            "ts": ts or "",
            "agent_id": agent_id or "",
        }

        vector_dict: dict[str, Any] = {"dense": list(embedding)}
        sparse = self._sparse_vector(text)
        if sparse is not None:
            vector_dict["sparse"] = sparse

        point = self._qmodels.PointStruct(
            id=_qdrant_point_id(user_id, session_id, kind, ref_id),
            vector=vector_dict,
            payload=payload,
        )
        self._client.upsert(collection_name=self._collection, points=[point])

    def search(
        self,
        *,
        user_id: str,
        q: str,
        embedding: list[float],
        kinds: Iterable[str],
        filters: dict[str, Any],
        limit: int,
    ) -> list[VectorMatch]:
        if not self.healthy() or len(embedding) != self.dim:
            return []
        kind_list = list(kinds)
        if not kind_list:
            return []

        must = [
            self._qmodels.FieldCondition(
                key="user_id",
                match=self._qmodels.MatchValue(value=user_id),
            ),
            self._qmodels.FieldCondition(
                key="kind",
                match=self._qmodels.MatchAny(any=kind_list),
            ),
        ]
        if filters.get("agent_id"):
            must.append(
                self._qmodels.FieldCondition(
                    key="agent_id",
                    match=self._qmodels.MatchValue(value=filters["agent_id"]),
                )
            )
        sids = filters.get("session_ids") or (
            [filters["session_id"]] if filters.get("session_id") else None
        )
        if sids:
            must.append(
                self._qmodels.FieldCondition(
                    key="session_id",
                    match=self._qmodels.MatchAny(any=list(sids)),
                )
            )
        if filters.get("from_ts") or filters.get("to_ts"):
            range_kwargs: dict[str, Any] = {}
            from_ts = _parse_qdrant_datetime(filters.get("from_ts"))
            to_ts = _parse_qdrant_datetime(filters.get("to_ts"))
            if from_ts:
                range_kwargs["gte"] = from_ts
            if to_ts:
                range_kwargs["lte"] = to_ts
            if not range_kwargs:
                return []
            must.append(
                self._qmodels.FieldCondition(
                    key="ts",
                    range=self._qmodels.DatetimeRange(**range_kwargs),
                )
            )
        flt = self._qmodels.Filter(must=must)

        # Dense + sparse hybrid via the Query API. When sparse embedder
        # isn't available we send dense-only.
        sparse = self._sparse_vector(q)
        prefetch: list[Any] = [
            self._qmodels.Prefetch(
                query=list(embedding),
                using="dense",
                limit=max(limit * 4, limit),
                filter=flt,
            )
        ]
        if sparse is not None:
            prefetch.append(
                self._qmodels.Prefetch(
                    query=sparse,
                    using="sparse",
                    limit=max(limit * 4, limit),
                    filter=flt,
                )
            )

        if hasattr(self._client, "query_points"):
            response = self._client.query_points(
                collection_name=self._collection,
                prefetch=prefetch,
                query=self._qmodels.FusionQuery(fusion=self._qmodels.Fusion.RRF),
                limit=limit,
                with_payload=True,
                query_filter=flt,
            )
            points = getattr(response, "points", response)
        else:  # pragma: no cover — older clients
            points = self._client.search(  # type: ignore[attr-defined]
                collection_name=self._collection,
                query_vector=("dense", list(embedding)),
                query_filter=flt,
                limit=limit,
                with_payload=True,
            )
        out: list[VectorMatch] = []
        for point in points:
            payload = point.payload or {}
            out.append(
                VectorMatch(
                    user_id=str(payload.get("user_id") or ""),
                    session_id=str(payload.get("session_id") or ""),
                    kind=str(payload.get("kind") or ""),
                    ref_id=str(payload.get("ref_id") or ""),
                    text=str(payload.get("text") or ""),
                    ts=(str(payload.get("ts")) if payload.get("ts") else None),
                    agent_id=(
                        str(payload.get("agent_id"))
                        if payload.get("agent_id")
                        else None
                    ),
                    score=float(point.score),
                )
            )
        return out

    def delete_session(self, *, user_id: str, session_id: str) -> int:
        if not self.healthy():
            return 0
        flt = self._qmodels.Filter(
            must=[
                self._qmodels.FieldCondition(
                    key="user_id",
                    match=self._qmodels.MatchValue(value=user_id),
                ),
                self._qmodels.FieldCondition(
                    key="session_id",
                    match=self._qmodels.MatchValue(value=session_id),
                ),
            ]
        )
        try:
            self._client.delete(
                collection_name=self._collection,
                points_selector=self._qmodels.FilterSelector(filter=flt),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant: delete_session failed (%s)", exc)
            return 0
        return 1

    def delete_ref(
        self, *, user_id: str, session_id: str, kind: str, ref_id: str
    ) -> None:
        if not self.healthy():
            return
        try:
            self._client.delete(
                collection_name=self._collection,
                points_selector=self._qmodels.PointIdsList(
                    points=[_qdrant_point_id(user_id, session_id, kind, ref_id)]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant: delete_ref failed (%s)", exc)

    def close(self) -> None:
        """Close the Qdrant client.

        Local-mode Qdrant holds an embedded SQLite handle; failing to
        close leaks the file descriptor and emits warnings on
        interpreter shutdown. Server-mode close is a noop on the
        underlying HTTP connection pool but harmless.
        """
        client = self._client
        self._client = None
        self._healthy = False
        if client is None:
            return
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant: client close failed (%s)", exc)


def build_vector_backend(*, db: Any, config: Any) -> VectorBackend:
    if not config.vector_enabled():
        return DisabledVectorBackend()

    if config.vector_provider == "pgvector":
        try:
            backend: VectorBackend = PgVectorBackend(
                db=db, model=config.embedding_model, dim=config.embedding_dim
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pgvector backend init failed (%s)", exc)
            raise RuntimeError("pgvector backend initialization failed") from exc
        if not backend.healthy():
            raise RuntimeError("pgvector backend is unavailable")
        return backend

    if config.vector_provider == "qdrant":
        backend = QdrantVectorBackend(
            url=config.qdrant_url,
            api_key=config.qdrant_api_key,
            collection=config.qdrant_collection,
            model=config.embedding_model,
            dim=config.embedding_dim,
            sparse_model=config.sparse_model,
        )
        if not backend.healthy():
            backend.close()
            raise RuntimeError("qdrant backend is unavailable")
        return backend

    return DisabledVectorBackend()


__all__ = [
    "VectorBackend",
    "VectorMatch",
    "DisabledVectorBackend",
    "PgVectorBackend",
    "QdrantVectorBackend",
    "build_vector_backend",
]
