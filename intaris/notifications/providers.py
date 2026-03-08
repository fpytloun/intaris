"""Notification provider implementations.

Each provider sends notifications to a specific platform (webhook,
Pushover, Slack). Providers implement a simple protocol:
- validate_config(): Check required config fields at channel creation
- send(): Deliver a notification (async, with single retry)

Adding a new provider:
1. Create a class implementing validate_config() and send()
2. Register it in the PROVIDERS dict at the bottom of this file
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from html import escape as html_escape
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Shared HTTP client for all providers (lazy-initialized)
_http_client: httpx.AsyncClient | None = None

# Default timeout for provider HTTP calls (seconds)
_DEFAULT_TIMEOUT = 10.0


async def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    return _http_client


async def close_client() -> None:
    """Close the shared HTTP client. Called during shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@dataclass
class Notification:
    """Notification payload for providers.

    Contains all information needed to format and send a notification.
    Providers select which fields to include based on their platform.
    """

    event_type: str  # "escalation", "resolution", or "session_suspended"
    call_id: str
    session_id: str
    user_id: str
    agent_id: str | None
    tool: str | None
    args_redacted: dict[str, Any] | None
    risk: str | None
    reasoning: str | None
    ui_url: str | None  # Link to Intaris UI approvals
    approve_url: str | None  # One-click approve (confirmation page)
    deny_url: str | None  # One-click deny (confirmation page)
    timestamp: str
    # Resolution-specific fields
    user_decision: str | None = None  # "approve" or "deny"
    user_note: str | None = None


class NotificationProvider(Protocol):
    """Protocol for notification providers.

    Providers must implement:
    - validate_config(): Verify required config fields
    - send(): Deliver a notification to the platform
    """

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        """Validate provider-specific configuration.

        Called during channel creation/update. Should raise ValueError
        if required fields are missing or invalid.
        """
        ...

    async def send(self, notification: Notification, config: dict[str, Any]) -> None:
        """Send a notification to the platform.

        Args:
            notification: The notification payload.
            config: Decrypted provider-specific configuration.

        Raises:
            Exception: On delivery failure (caller handles retry).
        """
        ...


class WebhookProvider:
    """Generic webhook notification provider.

    Sends JSON payloads to a user-configured URL. Supports optional
    HMAC-SHA256 signing (same pattern as the Cognis webhook).

    Config fields:
        url (required): Webhook endpoint URL.
        secret (optional): HMAC-SHA256 signing secret.
        headers (optional): Additional HTTP headers.
    """

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        """Validate webhook config."""
        if not config.get("url"):
            raise ValueError("Webhook config requires 'url' field")
        url = config["url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")

    async def send(self, notification: Notification, config: dict[str, Any]) -> None:
        """Send notification as JSON POST to the webhook URL."""
        client = await _get_client()

        payload: dict[str, Any] = {
            "event_type": notification.event_type,
            "call_id": notification.call_id,
            "session_id": notification.session_id,
            "user_id": notification.user_id,
            "agent_id": notification.agent_id,
            "tool": notification.tool,
            "risk": notification.risk,
            "reasoning": notification.reasoning,
            "timestamp": notification.timestamp,
        }

        if notification.ui_url:
            payload["ui_url"] = notification.ui_url
        if notification.approve_url:
            payload["approve_url"] = notification.approve_url
        if notification.deny_url:
            payload["deny_url"] = notification.deny_url
        if notification.user_decision:
            payload["user_decision"] = notification.user_decision
        if notification.user_note:
            payload["user_note"] = notification.user_note

        body = json.dumps(payload, separators=(",", ":")).encode()

        headers: dict[str, str] = {"Content-Type": "application/json"}

        # Optional HMAC signing
        secret = config.get("secret")
        if secret:
            signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Intaris-Signature"] = f"sha256={signature}"

        # Optional custom headers
        extra_headers = config.get("headers")
        if extra_headers and isinstance(extra_headers, dict):
            headers.update(extra_headers)

        response = await client.post(config["url"], content=body, headers=headers)
        response.raise_for_status()
        logger.info(
            "Webhook notification sent for call_id=%s (status=%d)",
            notification.call_id,
            response.status_code,
        )


class PushoverProvider:
    """Pushover push notification provider.

    Sends notifications via the Pushover API. Supports priority levels,
    device targeting, and custom sounds.

    Config fields:
        user_key (required): Pushover user/group key.
        app_token (required): Pushover application API token.
        priority (optional): Message priority (-2 to 2, default 1 = high).
        device (optional): Target device name.
        sound (optional): Notification sound name.
    """

    _API_URL = "https://api.pushover.net/1/messages.json"

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        """Validate Pushover config."""
        if not config.get("user_key"):
            raise ValueError("Pushover config requires 'user_key' field")
        if not config.get("app_token"):
            raise ValueError("Pushover config requires 'app_token' field")

    async def send(self, notification: Notification, config: dict[str, Any]) -> None:
        """Send notification via Pushover API."""
        client = await _get_client()

        if notification.event_type == "resolution":
            title = "Intaris: Escalation Resolved"
            message = self._format_resolution_message(notification)
        elif notification.event_type == "session_suspended":
            title = "Intaris: Session Suspended"
            message = self._format_escalation_message(notification)
        else:
            title = "Intaris: Escalation Required"
            message = self._format_escalation_message(notification)

        data: dict[str, Any] = {
            "token": config["app_token"],
            "user": config["user_key"],
            "title": title,
            "message": message,
            "priority": config.get("priority", 1),
            "html": 1,
        }

        # Set URL to the UI approvals page
        if notification.ui_url:
            data["url"] = notification.ui_url
            data["url_title"] = "Open in Intaris"

        # Optional fields
        if config.get("device"):
            data["device"] = config["device"]
        if config.get("sound"):
            data["sound"] = config["sound"]

        response = await client.post(self._API_URL, data=data)
        response.raise_for_status()
        logger.info(
            "Pushover notification sent for call_id=%s",
            notification.call_id,
        )

    @staticmethod
    def _format_escalation_message(n: Notification) -> str:
        """Format escalation message for Pushover (HTML mode)."""
        parts = []
        if n.tool:
            parts.append(f"<b>Tool:</b> {html_escape(n.tool)}")
        if n.risk:
            parts.append(f"<b>Risk:</b> {html_escape(n.risk)}")
        if n.reasoning:
            # Truncate reasoning for push notification
            reason = n.reasoning[:200]
            if len(n.reasoning) > 200:
                reason += "..."
            parts.append(f"\n{html_escape(reason)}")
        if n.approve_url:
            parts.append(f'\n<a href="{html_escape(n.approve_url)}">Approve</a>')
        if n.deny_url:
            parts.append(f' | <a href="{html_escape(n.deny_url)}">Deny</a>')
        return "\n".join(parts)

    @staticmethod
    def _format_resolution_message(n: Notification) -> str:
        """Format resolution message for Pushover (HTML mode)."""
        parts = []
        if n.tool:
            parts.append(f"<b>Tool:</b> {html_escape(n.tool)}")
        if n.user_decision:
            decision = html_escape(n.user_decision.upper())
            parts.append(f"<b>Decision:</b> {decision}")
        if n.user_note:
            parts.append(f"<b>Note:</b> {html_escape(n.user_note)}")
        return "\n".join(parts)


def _slack_escape(text: str) -> str:
    """Escape Slack mrkdwn special characters.

    Slack mrkdwn interprets ``&``, ``<``, and ``>`` as control characters.
    Escaping them prevents unintended formatting, link injection, or
    ``@here``/``@channel`` mentions from free-text fields.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackProvider:
    """Slack incoming webhook notification provider.

    Sends Block Kit formatted messages to a Slack channel via
    incoming webhook URL.

    Config fields:
        webhook_url (required): Slack incoming webhook URL.
    """

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        """Validate Slack config."""
        if not config.get("webhook_url"):
            raise ValueError("Slack config requires 'webhook_url' field")
        url = config["webhook_url"]
        if not url.startswith("https://hooks.slack.com/"):
            raise ValueError(
                "Slack webhook URL must start with https://hooks.slack.com/"
            )

    async def send(self, notification: Notification, config: dict[str, Any]) -> None:
        """Send notification via Slack incoming webhook."""
        client = await _get_client()

        if notification.event_type == "resolution":
            blocks = self._build_resolution_blocks(notification)
        elif notification.event_type == "session_suspended":
            blocks = self._build_escalation_blocks(notification)
        else:
            blocks = self._build_escalation_blocks(notification)

        payload = {"blocks": blocks}

        response = await client.post(
            config["webhook_url"],
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        logger.info(
            "Slack notification sent for call_id=%s",
            notification.call_id,
        )

    @staticmethod
    def _build_escalation_blocks(n: Notification) -> list[dict[str, Any]]:
        """Build Slack Block Kit blocks for escalation."""
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Intaris: Escalation Required",
                },
            },
        ]

        # Info section
        fields = []
        if n.tool:
            fields.append(
                {"type": "mrkdwn", "text": f"*Tool:* `{_slack_escape(n.tool)}`"}
            )
        if n.risk:
            fields.append(
                {"type": "mrkdwn", "text": f"*Risk:* {_slack_escape(n.risk)}"}
            )
        if n.session_id:
            fields.append(
                {
                    "type": "mrkdwn",
                    "text": f"*Session:* `{_slack_escape(n.session_id[:12])}...`",
                }
            )
        if fields:
            blocks.append({"type": "section", "fields": fields})

        # Reasoning
        if n.reasoning:
            reason = n.reasoning[:300]
            if len(n.reasoning) > 300:
                reason += "..."
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": _slack_escape(reason)},
                }
            )

        # Action buttons (links)
        actions = []
        if n.approve_url:
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "url": n.approve_url,
                    "style": "primary",
                }
            )
        if n.deny_url:
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "url": n.deny_url,
                    "style": "danger",
                }
            )
        if n.ui_url:
            actions.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open in Intaris"},
                    "url": n.ui_url,
                }
            )
        if actions:
            blocks.append({"type": "actions", "elements": actions})

        return blocks

    @staticmethod
    def _build_resolution_blocks(n: Notification) -> list[dict[str, Any]]:
        """Build Slack Block Kit blocks for resolution."""
        decision = _slack_escape((n.user_decision or "unknown").upper())
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Intaris: Escalation {decision}D",
                },
            },
        ]

        fields = []
        if n.tool:
            fields.append(
                {"type": "mrkdwn", "text": f"*Tool:* `{_slack_escape(n.tool)}`"}
            )
        if n.user_decision:
            fields.append({"type": "mrkdwn", "text": f"*Decision:* {decision}"})
        if fields:
            blocks.append({"type": "section", "fields": fields})

        if n.user_note:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Note:* {_slack_escape(n.user_note)}",
                    },
                }
            )

        return blocks


# ── Provider Registry ─────────────────────────────────────────────
# Adding a new provider: implement validate_config() + send(),
# then add it here.

PROVIDERS: dict[str, type] = {
    "webhook": WebhookProvider,
    "pushover": PushoverProvider,
    "slack": SlackProvider,
}
