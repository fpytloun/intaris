"""CRUD operations for notification channel configurations.

All operations are scoped by user_id for multi-tenant isolation.
Secrets (API keys, webhook URLs with tokens) are encrypted at rest
using Fernet, following the same pattern as mcp/store.py.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from intaris.db import Database

logger = logging.getLogger(__name__)

# Channel name validation: same pattern as MCP server names.
_CHANNEL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_CHANNEL_NAME_MAX_LEN = 64


class NotificationStore:
    """CRUD for notification channel configs with encrypted secrets.

    Encrypts provider config before storage, decrypts on internal read.
    API responses use redacted views (has_config flag instead of secrets).
    """

    def __init__(self, db: Database, encryption_key: str = ""):
        self._db = db
        self._encryption_key = encryption_key

    def _encrypt(self, plaintext: str) -> str:
        """Encrypt a JSON string for storage."""
        from intaris.crypto import encrypt

        return encrypt(plaintext, self._encryption_key)

    def _decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored JSON string."""
        from intaris.crypto import decrypt

        return decrypt(ciphertext, self._encryption_key)

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate channel name format."""
        if not name:
            raise ValueError("Channel name is required")
        if len(name) > _CHANNEL_NAME_MAX_LEN:
            raise ValueError(
                f"Channel name too long ({len(name)} chars, "
                f"max {_CHANNEL_NAME_MAX_LEN})"
            )
        if not _CHANNEL_NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid channel name '{name}'. Must match pattern: "
                "alphanumeric start, then alphanumeric/hyphen/underscore."
            )

    def upsert_channel(
        self,
        *,
        user_id: str,
        name: str,
        provider: str,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
        events: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a notification channel.

        Args:
            user_id: Tenant identifier.
            name: Channel name (unique per user).
            provider: Provider type (e.g., 'webhook', 'pushover', 'slack').
            config: Provider-specific configuration (encrypted at rest).
            enabled: Whether the channel is active.
            events: List of event types to receive (e.g.,
                ``["escalation", "denial", "session_suspended", "resolution"]``).
                When None, uses the default set (escalation, resolution,
                session_suspended). Denial is opt-in.

        Returns:
            The created/updated channel (redacted view).

        Raises:
            ValueError: If name is invalid or secrets require encryption key.
        """
        self._validate_name(name)

        # Validate provider is registered
        from intaris.notifications.providers import PROVIDERS

        if provider not in PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}'. "
                f"Available: {', '.join(sorted(PROVIDERS))}"
            )

        # Validate provider config
        provider_cls = PROVIDERS[provider]
        if config:
            provider_cls.validate_config(config)

        # Validate event types
        valid_events = {
            "escalation",
            "resolution",
            "session_suspended",
            "denial",
        }
        if events is not None:
            invalid = set(events) - valid_events
            if invalid:
                raise ValueError(
                    f"Invalid event types: {', '.join(sorted(invalid))}. "
                    f"Valid: {', '.join(sorted(valid_events))}"
                )

        # Encrypt config if provided
        config_encrypted = None
        if config:
            if not self._encryption_key:
                raise ValueError(
                    "INTARIS_ENCRYPTION_KEY is required to store notification "
                    "channel secrets. Set the environment variable and restart."
                )
            config_encrypted = self._encrypt(json.dumps(config))

        events_json = json.dumps(events) if events is not None else None
        now = datetime.now(timezone.utc).isoformat()

        with self._db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_channels
                    (user_id, name, provider, config_encrypted, enabled,
                     events, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, name) DO UPDATE SET
                    provider = excluded.provider,
                    config_encrypted = COALESCE(
                        excluded.config_encrypted,
                        notification_channels.config_encrypted
                    ),
                    enabled = excluded.enabled,
                    events = COALESCE(excluded.events,
                        notification_channels.events),
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    name,
                    provider,
                    config_encrypted,
                    int(enabled),
                    events_json,
                    now,
                    now,
                ),
            )

        return self.get_channel(user_id=user_id, name=name)

    def get_channel(
        self,
        *,
        user_id: str,
        name: str,
        decrypt_secrets: bool = False,
    ) -> dict[str, Any]:
        """Get a channel config by name.

        Args:
            user_id: Tenant identifier.
            name: Channel name.
            decrypt_secrets: If True, decrypt config (internal use only).

        Raises:
            ValueError: If channel not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT * FROM notification_channels WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"Notification channel '{name}' not found")

        return self._row_to_dict(row, decrypt_secrets=decrypt_secrets)

    def list_channels(
        self,
        *,
        user_id: str,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List all notification channels for a user.

        Returns redacted view (no decrypted secrets).
        """
        with self._db.cursor() as cur:
            if enabled_only:
                cur.execute(
                    "SELECT * FROM notification_channels "
                    "WHERE user_id = ? AND enabled = 1 "
                    "ORDER BY name",
                    (user_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM notification_channels "
                    "WHERE user_id = ? ORDER BY name",
                    (user_id,),
                )
            rows = cur.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def delete_channel(self, *, user_id: str, name: str) -> None:
        """Delete a notification channel.

        Raises:
            ValueError: If channel not found.
        """
        with self._db.cursor() as cur:
            cur.execute(
                "DELETE FROM notification_channels WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Notification channel '{name}' not found")

    def record_send_result(
        self,
        *,
        user_id: str,
        name: str,
        success: bool,
    ) -> None:
        """Record the result of a notification send attempt.

        Updates last_success_at on success, increments failure_count
        on failure (resets to 0 on success).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._db.cursor() as cur:
            if success:
                cur.execute(
                    "UPDATE notification_channels "
                    "SET last_success_at = ?, failure_count = 0, updated_at = ? "
                    "WHERE user_id = ? AND name = ?",
                    (now, now, user_id, name),
                )
            else:
                cur.execute(
                    "UPDATE notification_channels "
                    "SET failure_count = failure_count + 1, updated_at = ? "
                    "WHERE user_id = ? AND name = ?",
                    (now, user_id, name),
                )

    def _row_to_dict(
        self, row: Any, *, decrypt_secrets: bool = False
    ) -> dict[str, Any]:
        """Convert a sqlite3.Row to a dict with optional secret decryption."""
        d = dict(row)

        # Handle secrets: decrypt or redact
        if decrypt_secrets and self._encryption_key:
            if d.get("config_encrypted"):
                try:
                    d["config"] = json.loads(self._decrypt(d["config_encrypted"]))
                except (ValueError, json.JSONDecodeError):
                    logger.warning(
                        "Failed to decrypt config for channel %s", d.get("name")
                    )
                    d["config"] = None
            else:
                d["config"] = None
        else:
            # Redacted view: show boolean flag
            d["has_config"] = bool(d.get("config_encrypted"))

        # Remove encrypted field from output
        d.pop("config_encrypted", None)

        # Convert enabled from int to bool
        d["enabled"] = bool(d.get("enabled", 0))

        # Parse events JSON array
        if d.get("events"):
            try:
                d["events"] = json.loads(d["events"])
            except (json.JSONDecodeError, TypeError):
                d["events"] = None

        return d
