"""Notification channel management endpoints.

CRUD for per-user notification channels + test endpoint.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone as tz
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from intaris.api.deps import SessionContext, get_session_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications")


# ── Request/Response Models ───────────────────────────────────────


class ChannelRequest(BaseModel):
    """Request to create or update a notification channel."""

    provider: str = Field(..., description="Provider type: webhook, pushover, slack")
    config: dict[str, Any] | None = Field(
        None, description="Provider-specific configuration"
    )
    enabled: bool = Field(True, description="Whether the channel is active")
    events: list[str] | None = Field(
        None,
        description="Event types to receive: escalation, resolution, "
        "session_suspended, denial. Null = default set (no denial).",
    )


class ChannelResponse(BaseModel):
    """Notification channel details (secrets redacted)."""

    name: str
    provider: str
    enabled: bool
    has_config: bool = False
    events: list[str] | None = None
    last_success_at: str | None = None
    failure_count: int = 0
    created_at: str
    updated_at: str


class ChannelListResponse(BaseModel):
    """List of notification channels."""

    items: list[ChannelResponse]


class TestResponse(BaseModel):
    """Response from test notification."""

    ok: bool = True
    message: str = "Test notification sent"


# ── Endpoints ─────────────────────────────────────────────────────


@router.get("/channels", response_model=ChannelListResponse)
async def list_channels(
    ctx: SessionContext = Depends(get_session_context),
) -> ChannelListResponse:
    """List all notification channels for the current user."""
    from intaris.notifications.store import NotificationStore
    from intaris.server import _get_config, _get_db

    try:
        cfg = _get_config()
        store = NotificationStore(_get_db(), cfg.mcp.encryption_key)
        channels = store.list_channels(user_id=ctx.user_id)
        return ChannelListResponse(items=[ChannelResponse(**ch) for ch in channels])
    except Exception:
        logger.exception("Error listing notification channels")
        raise HTTPException(
            status_code=500,
            detail="Internal error listing notification channels",
        )


@router.get("/channels/{name}", response_model=ChannelResponse)
async def get_channel(
    name: str,
    ctx: SessionContext = Depends(get_session_context),
) -> ChannelResponse:
    """Get a notification channel by name."""
    from intaris.notifications.store import NotificationStore
    from intaris.server import _get_config, _get_db

    try:
        cfg = _get_config()
        store = NotificationStore(_get_db(), cfg.mcp.encryption_key)
        channel = store.get_channel(user_id=ctx.user_id, name=name)
        return ChannelResponse(**channel)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error getting notification channel")
        raise HTTPException(
            status_code=500,
            detail="Internal error getting notification channel",
        )


@router.put("/channels/{name}", response_model=ChannelResponse)
async def upsert_channel(
    name: str,
    request: ChannelRequest,
    ctx: SessionContext = Depends(get_session_context),
) -> ChannelResponse:
    """Create or update a notification channel."""
    from intaris.notifications.store import NotificationStore
    from intaris.server import _get_config, _get_db

    try:
        cfg = _get_config()
        store = NotificationStore(_get_db(), cfg.mcp.encryption_key)
        channel = store.upsert_channel(
            user_id=ctx.user_id,
            name=name,
            provider=request.provider,
            config=request.config,
            enabled=request.enabled,
            events=request.events,
        )
        return ChannelResponse(**channel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception:
        logger.exception("Error upserting notification channel")
        raise HTTPException(
            status_code=500,
            detail="Internal error saving notification channel",
        )


@router.delete("/channels/{name}")
async def delete_channel(
    name: str,
    ctx: SessionContext = Depends(get_session_context),
) -> dict[str, bool]:
    """Delete a notification channel."""
    from intaris.notifications.store import NotificationStore
    from intaris.server import _get_config, _get_db

    try:
        cfg = _get_config()
        store = NotificationStore(_get_db(), cfg.mcp.encryption_key)
        store.delete_channel(user_id=ctx.user_id, name=name)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception:
        logger.exception("Error deleting notification channel")
        raise HTTPException(
            status_code=500,
            detail="Internal error deleting notification channel",
        )


@router.post("/channels/{name}/test", response_model=TestResponse)
async def test_channel(
    name: str,
    http_request: Request,
    ctx: SessionContext = Depends(get_session_context),
) -> TestResponse:
    """Send a test notification to a channel.

    Creates a synthetic escalation notification to verify the channel
    configuration works correctly.
    """
    from intaris.notifications.providers import Notification

    dispatcher = getattr(http_request.app.state, "notification_dispatcher", None)
    if dispatcher is None:
        raise HTTPException(
            status_code=503,
            detail="Notification dispatcher not initialized",
        )

    # Create synthetic test notification
    notification = Notification(
        event_type="escalation",
        call_id="test-notification",
        session_id="test-session",
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        tool="test_tool",
        args_redacted={"command": "echo hello"},
        risk="medium",
        reasoning="This is a test notification to verify your channel configuration.",
        ui_url=None,
        approve_url=None,
        deny_url=None,
        timestamp=datetime.now(tz.utc).isoformat(),
    )

    # Send to the specific channel via the public test method
    try:
        await dispatcher.send_test(
            user_id=ctx.user_id,
            channel_name=name,
            notification=notification,
        )
        return TestResponse(ok=True, message="Test notification sent successfully")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception("Test notification failed for channel '%s'", name)
        raise HTTPException(
            status_code=502,
            detail=f"Test notification failed: {e}",
        ) from e
