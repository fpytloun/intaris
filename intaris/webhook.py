"""Webhook client for escalation notifications.

Sends HMAC-signed HTTP POST requests to a configured webhook URL when
tool call evaluations result in escalation decisions. This enables
Cognis (or any external system) to populate an approval queue.

Delivery is fire-and-forget — errors are logged but never block the
evaluation pipeline. A single retry with 1s delay is attempted on failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from intaris.config import WebhookConfig

logger = logging.getLogger(__name__)


class WebhookClient:
    """Async webhook client for escalation notifications.

    Sends HMAC-SHA256 signed payloads to the configured webhook URL.
    Uses a persistent httpx.AsyncClient for connection pooling.

    Args:
        config: Webhook configuration with URL, secret, and timeout.
    """

    def __init__(self, config: WebhookConfig):
        self._url = config.url
        self._secret = config.secret
        self._timeout = config.timeout_ms / 1000
        self._base_url = config.base_url
        self._client: httpx.AsyncClient | None = None

    def is_configured(self) -> bool:
        """Check if webhook delivery is configured."""
        return bool(self._url)

    async def send_escalation(
        self,
        *,
        call_id: str,
        session_id: str,
        user_id: str,
        agent_id: str | None,
        tool: str | None,
        args_redacted: dict[str, Any] | None,
        risk: str | None,
        reasoning: str | None,
    ) -> None:
        """Send an escalation notification to the webhook URL.

        Fire-and-forget: errors are logged but never raised. A single
        retry with 1s delay is attempted on failure.

        Args:
            call_id: Unique call identifier.
            session_id: Session the call belongs to.
            user_id: Tenant identifier.
            agent_id: Agent making the call.
            tool: Tool name.
            args_redacted: Redacted tool arguments.
            risk: Risk level.
            reasoning: Evaluation reasoning.
        """
        if not self._url:
            return

        payload = {
            "type": "escalation",
            "call_id": call_id,
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "tool": tool,
            "args_redacted": args_redacted,
            "risk": risk,
            "reasoning": reasoning,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Construct intaris_url for the audit record in the UI
        if self._base_url:
            payload["intaris_url"] = f"{self._base_url.rstrip('/')}/ui/audit/{call_id}"

        try:
            await self._deliver(payload)
        except Exception:
            # First attempt failed — retry once after 1s
            logger.warning("Webhook delivery failed, retrying in 1s")
            try:
                await asyncio.sleep(1)
                await self._deliver(payload)
            except Exception:
                logger.exception(
                    "Webhook delivery failed after retry for call_id=%s", call_id
                )

    async def _deliver(self, payload: dict[str, Any]) -> None:
        """Send the payload to the webhook URL with HMAC signature."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        body = json.dumps(payload, separators=(",", ":")).encode()
        signature = self._sign(body)

        response = await self._client.post(
            self._url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Intaris-Signature": f"sha256={signature}",
            },
        )
        response.raise_for_status()
        logger.info(
            "Webhook delivered for call_id=%s (status=%d)",
            payload.get("call_id"),
            response.status_code,
        )

    def _sign(self, body: bytes) -> str:
        """Compute HMAC-SHA256 signature for the payload."""
        return hmac.new(self._secret.encode(), body, hashlib.sha256).hexdigest()

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
