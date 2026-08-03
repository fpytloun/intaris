"""Search API endpoints.

Three search kinds: ``summary``, ``intention``, ``reasoning``. The
lexical tier is always available when the master flag is on (queries
the canonical Intaris tables directly). The optional vector tier
adds semantic / multilingual recall when configured.

Sync DB / embedding work runs in a worker thread (``asyncio.to_thread``)
so slow queries never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from intaris.api.deps import SessionContext, get_session_context
from intaris.search.types import (
    SEARCH_KINDS,
    SearchBackends,
    SearchHealth,
    SearchRequest,
    SearchResponse,
    SearchSessionsRequest,
    SearchSessionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _service_or_none(request: Request) -> Any:
    return getattr(request.app.state, "search_service", None)


def _service(request: Request) -> Any:
    svc = _service_or_none(request)
    if svc is None and getattr(request.app.state, "search_initializing", False):
        raise HTTPException(status_code=503, detail="search_initializing")
    if svc is None or not svc.enabled:
        raise HTTPException(status_code=404, detail="search_disabled")
    return svc


@router.get("/search/health", response_model=SearchHealth)
async def search_health(request: Request) -> SearchHealth:
    svc = _service_or_none(request)
    if svc is None:
        from intaris.search.types import (
            SearchLexicalCapabilities,
            SearchVectorState,
        )

        initializing = getattr(request.app.state, "search_initializing", False)
        return SearchHealth(
            enabled=initializing,
            lexical=SearchLexicalCapabilities(backend="disabled"),
            vector=SearchVectorState(provider="disabled"),
            notes=["Search initialization in progress"] if initializing else [],
        )
    return await asyncio.to_thread(svc.health)


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> SearchResponse:
    svc = _service(request)
    if not body.q or not body.q.strip():
        raise HTTPException(status_code=400, detail="empty_query")
    if body.limit <= 0 or body.limit > 200:
        raise HTTPException(status_code=400, detail="invalid_limit")

    filters = _resolve_filters(body.filters)
    matches, next_cursor, mode_used, degraded = await asyncio.to_thread(
        svc.search,
        user_id=ctx.user_id,
        q=body.q,
        kinds=body.kinds,
        filters=filters,
        mode=body.mode,
        limit=body.limit,
        cursor=body.cursor,
    )
    response = SearchResponse(
        matches=matches,
        next_cursor=next_cursor,
        backend=SearchBackends(
            lexical=svc.lexical_backend,
            vector=svc.vector_backend_name,
            mode_used=mode_used,
        ),
    )
    if degraded:
        request.state.search_degraded = degraded
    return response


@router.post("/search/sessions", response_model=SearchSessionsResponse)
async def search_sessions(
    body: SearchSessionsRequest,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> SearchSessionsResponse:
    svc = _service(request)
    if not body.q or not body.q.strip():
        raise HTTPException(status_code=400, detail="empty_query")
    if body.limit <= 0 or body.limit > 100:
        raise HTTPException(status_code=400, detail="invalid_limit")

    filters = _resolve_filters(body.filters)
    sessions, next_cursor, mode_used, degraded = await asyncio.to_thread(
        svc.search_sessions,
        user_id=ctx.user_id,
        q=body.q,
        kinds=body.kinds,
        filters=filters,
        mode=body.mode,
        limit=body.limit,
        cursor=body.cursor,
    )
    response = SearchSessionsResponse(
        sessions=sessions,
        next_cursor=next_cursor,
        backend=SearchBackends(
            lexical=svc.lexical_backend,
            vector=svc.vector_backend_name,
            mode_used=mode_used,
        ),
    )
    if degraded:
        request.state.search_degraded = degraded
    return response


@router.post("/search/reindex")
async def trigger_reindex(
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> dict[str, Any]:
    svc = _service(request)
    try:
        job_id = await svc.trigger_reindex(reason="manual")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"job_id": job_id, "status": "queued"}


@router.get("/search/reindex/{job_id}")
async def reindex_status(
    job_id: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> dict[str, Any]:
    svc = _service(request)
    health = await asyncio.to_thread(svc.health)
    if health.vector.backfill_job_id != job_id:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {
        "job_id": job_id,
        "state": health.vector.backfill_status,
        "total": health.vector.backfill_total or 0,
        "processed": health.vector.backfill_processed or 0,
        "error": None,
    }


@router.get("/search/config")
async def search_config(
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> dict[str, Any]:
    svc = _service(request)
    health = await asyncio.to_thread(svc.health)
    return {
        "enabled": health.enabled,
        "lexical": health.lexical.model_dump(),
        "vector": health.vector.model_dump(),
        "indexable_kinds": sorted(SEARCH_KINDS),
        "notes": health.notes,
    }


def _resolve_filters(req_filters: Any) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if req_filters is None:
        return filters
    if getattr(req_filters, "agent_id", None):
        filters["agent_id"] = req_filters.agent_id
    if getattr(req_filters, "session_id", None):
        filters["session_id"] = req_filters.session_id
    if getattr(req_filters, "session_ids", None):
        filters["session_ids"] = list(req_filters.session_ids)
    if getattr(req_filters, "from_ts", None):
        filters["from_ts"] = req_filters.from_ts
    if getattr(req_filters, "to_ts", None):
        filters["to_ts"] = req_filters.to_ts
    return filters


__all__ = ["router"]
