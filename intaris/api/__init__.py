"""REST API sub-application for intaris.

Provides OpenAPI-documented endpoints for safety evaluation,
session management, and audit log access.
"""

from __future__ import annotations

from fastapi import FastAPI

from intaris import __version__


def create_api_app() -> FastAPI:
    """Create the FastAPI sub-application for REST API.

    Routers are imported here (not at module level) to avoid circular
    imports — route handlers reference server.py globals.
    """
    app = FastAPI(
        title="intaris",
        description="Guardrails for AI coding agents — REST API",
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    from intaris.api.analysis import router as analysis_router
    from intaris.api.audit import router as audit_router
    from intaris.api.evaluate import router as evaluate_router
    from intaris.api.info import router as info_router
    from intaris.api.intention import router as intention_router
    from intaris.api.mcp import router as mcp_router

    app.include_router(evaluate_router, tags=["evaluate"])
    app.include_router(intention_router, tags=["sessions"])
    app.include_router(audit_router, tags=["audit"])
    app.include_router(info_router, tags=["info"])
    app.include_router(mcp_router, tags=["mcp"])
    app.include_router(analysis_router, tags=["analysis"])

    return app
