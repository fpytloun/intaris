"""Notification dispatcher — routes notifications to user's channels.

Loads enabled channels for a user from the database, instantiates
the appropriate provider for each channel, and dispatches notifications.
All sends are fire-and-forget with single retry on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from intaris.db import Database
from intaris.notifications.providers import (
    PROVIDERS,
    Notification,
    close_client,
)
from intaris.notifications.store import NotificationStore
from intaris.notifications.tokens import generate_action_urls

logger = logging.getLogger(__name__)

# Re-export Notification for convenience
__all__ = ["Notification", "NotificationDispatcher"]

# Default event types when channel has no explicit events config.
# Denial and judge_approval are opt-in — users must explicitly add them
# to avoid noise.  Judge denial/deferral/error are included because they
# represent the final outcome of judge-reviewed escalations and may
# require human attention.
DEFAULT_CHANNEL_EVENTS = {
    "escalation",
    "resolution",
    "session_suspended",
    "judge_denial",
    "judge_deferral",
    "judge_error",
}

# Judge event types that map to a "parent" event type for backward
# compatibility.  When a channel has an explicit events list that
# includes the parent type but none of the judge-specific types, the
# judge event is accepted as a fallback.  This prevents silent
# notification loss for channels configured before judge event types
# existed.
_JUDGE_EVENT_FALLBACK: dict[str, str] = {
    "judge_denial": "resolution",
    "judge_approval": "resolution",
    "judge_deferral": "escalation",
    "judge_error": "escalation",
}

# Event types that represent pending escalations needing human action
# (approve/deny links should be generated).
_ACTIONABLE_EVENT_TYPES = {"escalation", "judge_deferral", "judge_error"}


class NotificationDispatcher:
    """Routes notifications to user's configured channels.

    Loads enabled channels from the database, instantiates providers,
    and dispatches notifications. Each send is independent — failures
    on one channel don't affect others.

    Args:
        db: Database instance.
        encryption_key: Fernet key for decrypting channel secrets.
        base_url: Intaris base URL for constructing action URLs.
    """

    def __init__(
        self,
        db: Database,
        encryption_key: str = "",
        base_url: str = "",
    ):
        self._store = NotificationStore(db, encryption_key)
        self._encryption_key = encryption_key
        self._base_url = base_url

    async def notify(
        self,
        *,
        user_id: str,
        notification: Notification,
    ) -> None:
        """Send notification to all enabled channels for a user.

        Fire-and-forget: errors are logged but never raised.
        Each channel gets its own send attempt with single retry.

        Args:
            user_id: Tenant identifier.
            notification: The notification payload.
        """
        try:
            channels = self._store.list_channels(user_id=user_id, enabled_only=True)
        except Exception:
            logger.exception(
                "Failed to load notification channels for user=%s", user_id
            )
            return

        if not channels:
            return

        # Enrich notification with action URLs if possible
        if (
            self._base_url
            and self._encryption_key
            and notification.event_type in _ACTIONABLE_EVENT_TYPES
        ):
            try:
                approve_url, deny_url = generate_action_urls(
                    call_id=notification.call_id,
                    user_id=user_id,
                    base_url=self._base_url,
                    encryption_key=self._encryption_key,
                )
                notification.approve_url = approve_url
                notification.deny_url = deny_url
            except Exception:
                logger.warning(
                    "Failed to generate action URLs for call_id=%s",
                    notification.call_id,
                )

            # Set UI URL
            notification.ui_url = (
                f"{self._base_url.rstrip('/')}/ui/"
                f"#approvals?call_id={notification.call_id}"
            )

        # Enrich behavioral analysis notifications with UI URLs
        if self._base_url and notification.event_type == "summary_alert":
            notification.ui_url = (
                f"{self._base_url.rstrip('/')}/ui/"
                f"#sessions?session_id={notification.session_id}"
            )
        elif self._base_url and notification.event_type == "analysis_alert":
            notification.ui_url = f"{self._base_url.rstrip('/')}/ui/#analysis"

        # Dispatch to each channel concurrently (filtered by event type)
        tasks = []
        for channel in channels:
            if not self._channel_accepts_event(channel, notification.event_type):
                continue
            tasks.append(
                self._send_to_channel(
                    user_id=user_id,
                    channel=channel,
                    notification=notification,
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _channel_accepts_event(channel: dict[str, Any], event_type: str) -> bool:
        """Check if a channel is configured to receive this event type.

        Channels can specify an ``events`` field (JSON array of event type
        strings). When absent or null, the channel uses the default set
        which includes escalation, resolution, session_suspended, and
        judge outcome events (judge_denial, judge_deferral, judge_error).

        For backward compatibility, channels with explicit event lists
        that include a "parent" type (e.g. ``resolution``) but none of
        the judge-specific types will still receive the corresponding
        judge events.  This prevents silent notification loss for
        channels configured before judge event types were introduced.
        """
        events_raw = channel.get("events")
        if not events_raw:
            return event_type in DEFAULT_CHANNEL_EVENTS

        # Parse JSON array if stored as string
        if isinstance(events_raw, str):
            try:
                events = set(json.loads(events_raw))
            except (json.JSONDecodeError, TypeError):
                return event_type in DEFAULT_CHANNEL_EVENTS
        elif isinstance(events_raw, list):
            events = set(events_raw)
        else:
            return event_type in DEFAULT_CHANNEL_EVENTS

        if event_type in events:
            return True

        # Backward compatibility: accept judge events when the channel
        # subscribes to the parent type but has no explicit judge types.
        fallback_parent = _JUDGE_EVENT_FALLBACK.get(event_type)
        if fallback_parent and fallback_parent in events:
            # Only fall back when the channel has NO judge-specific types
            # at all — once a user adds any judge_* type, they've opted
            # into granular control and the fallback is disabled.
            has_any_judge = bool(events & set(_JUDGE_EVENT_FALLBACK))
            if not has_any_judge:
                logger.debug(
                    "Channel '%s' accepted '%s' via fallback (has '%s')",
                    channel.get("name", "?"),
                    event_type,
                    fallback_parent,
                )
                return True

        logger.debug(
            "Channel '%s' filtered out event_type='%s'",
            channel.get("name", "?"),
            event_type,
        )
        return False

    async def _send_to_channel(
        self,
        *,
        user_id: str,
        channel: dict[str, Any],
        notification: Notification,
    ) -> None:
        """Send notification to a single channel with retry.

        Single retry with 1s delay on failure (matches webhook.py pattern).
        Records send result for channel health tracking.
        """
        channel_name = channel.get("name", "unknown")
        provider_name = channel.get("provider", "unknown")

        provider_cls = PROVIDERS.get(provider_name)
        if provider_cls is None:
            logger.warning(
                "Unknown provider '%s' for channel '%s'",
                provider_name,
                channel_name,
            )
            return

        # Get decrypted config
        try:
            full_channel = self._store.get_channel(
                user_id=user_id,
                name=channel_name,
                decrypt_secrets=True,
            )
            config = full_channel.get("config") or {}
        except Exception:
            logger.exception("Failed to decrypt config for channel '%s'", channel_name)
            self._store.record_send_result(
                user_id=user_id, name=channel_name, success=False
            )
            return

        provider = provider_cls()

        try:
            await provider.send(notification, config)
            self._store.record_send_result(
                user_id=user_id, name=channel_name, success=True
            )
        except Exception:
            # First attempt failed — retry once after 1s
            logger.warning(
                "Notification delivery failed for channel '%s', retrying in 1s",
                channel_name,
            )
            try:
                await asyncio.sleep(1)
                await provider.send(notification, config)
                self._store.record_send_result(
                    user_id=user_id, name=channel_name, success=True
                )
            except Exception:
                logger.exception(
                    "Notification delivery failed after retry for "
                    "channel='%s' call_id=%s",
                    channel_name,
                    notification.call_id,
                )
                self._store.record_send_result(
                    user_id=user_id, name=channel_name, success=False
                )

    async def send_test(
        self,
        *,
        user_id: str,
        channel_name: str,
        notification: Notification,
    ) -> None:
        """Send a test notification to a single channel.

        Unlike ``notify()``, this raises on failure so the caller
        can report the error to the user.

        Args:
            user_id: Tenant identifier.
            channel_name: Name of the channel to test.
            notification: The notification payload.
        """
        channel = self._store.get_channel(
            user_id=user_id,
            name=channel_name,
            decrypt_secrets=True,
        )
        await self._send_to_channel(
            user_id=user_id,
            channel=channel,
            notification=notification,
        )

    async def close(self) -> None:
        """Close the shared HTTP client."""
        await close_client()
