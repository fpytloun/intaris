"""REST API endpoints for MCP server management.

Provides CRUD for upstream MCP server configurations and per-tool
preference overrides. Used by the management UI and external tools.

All operations are scoped by user_id from the authenticated session.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from intaris.api.deps import SessionContext, get_session_context
from intaris.mcp.store import MCPServerStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp")


# ── Pydantic Models ──────────────────────────────────────────────────


class ServerCreateRequest(BaseModel):
    """Request to create or update an MCP server configuration."""

    name: str = Field(
        ..., description="Server name (alphanumeric, hyphens, underscores)"
    )
    transport: Literal["stdio", "streamable-http", "sse"] = Field(
        ..., description="Transport type"
    )
    command: str | None = Field(None, description="Command for stdio transport")
    args: list[str] | None = Field(None, description="Args for stdio transport")
    env: dict[str, str] | None = Field(
        None, description="Environment variables (encrypted at rest)"
    )
    cwd: str | None = Field(None, description="Working directory for stdio")
    url: str | None = Field(None, description="URL for HTTP/SSE transport")
    headers: dict[str, str] | None = Field(
        None, description="HTTP headers (encrypted at rest)"
    )
    agent_pattern: str = Field("*", description="Agent pattern (fnmatch glob)")
    enabled: bool = Field(True, description="Whether the server is enabled")


class ServerResponse(BaseModel):
    """MCP server configuration (redacted view)."""

    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    url: str | None = None
    agent_pattern: str = "*"
    enabled: bool = True
    source: str = "api"
    server_instructions: str | None = None
    tools_cache: list[dict[str, Any]] | None = None
    tools_cache_at: str | None = None
    has_env: bool = False
    has_headers: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class ServerListResponse(BaseModel):
    """List of MCP server configurations."""

    items: list[ServerResponse]


class ToolPreferenceRequest(BaseModel):
    """Request to set a tool preference override."""

    preference: Literal["auto-approve", "evaluate", "escalate", "deny"] = Field(
        ..., description="Tool preference"
    )


class ToolPreferenceResponse(BaseModel):
    """Tool preferences for a server."""

    preferences: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of tool_name → preference",
    )


# ── Helper ───────────────────────────────────────────────────────────


def _get_server_store() -> MCPServerStore:
    """Get the MCPServerStore instance."""
    from intaris.server import _get_config, _get_db

    cfg = _get_config()
    return MCPServerStore(_get_db(), cfg.mcp.encryption_key)


def _friendly_connection_error(exc: Exception) -> str:
    """Extract a user-friendly message from MCP connection errors.

    Common MCP SDK / transport errors are mapped to actionable messages.
    Falls back to ``str(exc)`` for unknown errors.
    """
    msg = str(exc)

    # ConnectionError from our _connect() unwrapper already has context.
    if isinstance(exc, ConnectionError):
        return msg

    # httpx.HTTPStatusError — wrong transport type or auth failure.
    if "HTTPStatusError" in type(exc).__name__ or "HTTPStatusError" in msg:
        if "405" in msg:
            return f"{msg} — the server may use a different transport type (try SSE instead of HTTP or vice versa)"
        return msg

    # Common socket-level errors.
    if isinstance(exc, ConnectionRefusedError):
        return f"Connection refused — is the server running? ({msg})"
    if isinstance(exc, TimeoutError):
        return f"Connection timed out ({msg})"
    if isinstance(exc, OSError) and "Name or service not known" in msg:
        return f"DNS resolution failed — check the server URL ({msg})"

    # Truncate overly long exception messages (e.g. full tracebacks).
    if len(msg) > 300:
        msg = msg[:300] + "…"

    return msg


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/servers", response_model=ServerListResponse)
async def list_servers(
    ctx: SessionContext = Depends(get_session_context),
    enabled_only: bool = False,
):
    """List all configured MCP servers for the current user."""
    store = _get_server_store()
    try:
        servers = store.list_servers(
            user_id=ctx.user_id,
            enabled_only=enabled_only,
        )
        return ServerListResponse(items=servers)
    except Exception as exc:
        logger.exception("Failed to list MCP servers")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/servers/{name}", response_model=ServerResponse)
async def get_server(
    name: str,
    ctx: SessionContext = Depends(get_session_context),
):
    """Get a specific MCP server configuration."""
    store = _get_server_store()
    try:
        server = store.get_server(user_id=ctx.user_id, name=name)
        return server
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get MCP server %s", name)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/servers/{name}", response_model=ServerResponse)
async def upsert_server(
    name: str,
    body: ServerCreateRequest,
    ctx: SessionContext = Depends(get_session_context),
):
    """Create or update an MCP server configuration.

    The name in the URL must match the name in the request body.
    """
    if body.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"URL name '{name}' does not match body name '{body.name}'",
        )

    store = _get_server_store()
    try:
        server = store.upsert_server(
            user_id=ctx.user_id,
            name=body.name,
            transport=body.transport,
            command=body.command,
            args=body.args,
            env=body.env,
            cwd=body.cwd,
            url=body.url,
            headers=body.headers,
            agent_pattern=body.agent_pattern,
            enabled=body.enabled,
            source="api",
        )
        return server
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to upsert MCP server %s", name)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/servers/{name}")
async def delete_server(
    name: str,
    ctx: SessionContext = Depends(get_session_context),
):
    """Delete an MCP server configuration.

    Also deletes all associated tool preferences (cascade).
    """
    store = _get_server_store()
    try:
        store.delete_server(user_id=ctx.user_id, name=name)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to delete MCP server %s", name)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/servers/{name}/refresh")
async def refresh_server_tools(
    name: str,
    request: Request,
    ctx: SessionContext = Depends(get_session_context),
):
    """Force-refresh the tools cache for an MCP server.

    Connects to the upstream server, fetches the current tool list,
    and updates the cache. Returns the refreshed tool list.
    """
    from intaris.mcp.proxy import MCPProxy

    mcp_proxy: MCPProxy | None = getattr(request.app.state, "mcp_proxy", None)
    if mcp_proxy is None:
        raise HTTPException(
            status_code=503,
            detail="MCP proxy is not available",
        )

    try:
        tools = await mcp_proxy.refresh_server_tools(
            user_id=ctx.user_id,
            server_name=name,
        )
        return {"tools": tools, "count": len(tools)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to refresh tools for server %s", name)
        raise HTTPException(
            status_code=502, detail=_friendly_connection_error(exc)
        ) from exc


# ── Tool Preferences ─────────────────────────────────────────────────


@router.get(
    "/servers/{server_name}/preferences",
    response_model=ToolPreferenceResponse,
)
async def get_tool_preferences(
    server_name: str,
    ctx: SessionContext = Depends(get_session_context),
):
    """Get all tool preferences for a server."""
    store = _get_server_store()
    try:
        prefs = store.get_tool_preferences(
            user_id=ctx.user_id,
            server_name=server_name,
        )
        return ToolPreferenceResponse(preferences=prefs)
    except Exception as exc:
        logger.exception("Failed to get tool preferences for %s", server_name)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/servers/{server_name}/preferences/{tool_name}")
async def set_tool_preference(
    server_name: str,
    tool_name: str,
    body: ToolPreferenceRequest,
    ctx: SessionContext = Depends(get_session_context),
):
    """Set a tool preference override."""
    store = _get_server_store()
    try:
        store.set_tool_preference(
            user_id=ctx.user_id,
            server_name=server_name,
            tool_name=tool_name,
            preference=body.preference,
        )
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Failed to set tool preference for %s:%s", server_name, tool_name
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/servers/{server_name}/preferences/{tool_name}")
async def delete_tool_preference(
    server_name: str,
    tool_name: str,
    ctx: SessionContext = Depends(get_session_context),
):
    """Reset a tool preference to default (evaluate)."""
    store = _get_server_store()
    try:
        store.delete_tool_preference(
            user_id=ctx.user_id,
            server_name=server_name,
            tool_name=tool_name,
        )
        return {"ok": True}
    except Exception as exc:
        logger.exception(
            "Failed to delete tool preference for %s:%s", server_name, tool_name
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
