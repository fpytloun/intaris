"""Notification dispatcher — routes notifications to user's channels.

Loads enabled channels for a user from the database, instantiates
the appropriate provider for each channel, and dispatches notifications.
All sends are fire-and-forget with single retry on failure.
"""

from __future__ import annotations

import asyncio
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
            and notification.event_type == "escalation"
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

        # Dispatch to each channel concurrently
        tasks = []
        for channel in channels:
            tasks.append(
                self._send_to_channel(
                    user_id=user_id,
                    channel=channel,
                    notification=notification,
                )
            )

        await asyncio.gather(*tasks, return_exceptions=True)

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
