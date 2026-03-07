"""Tests for the webhook client."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx

from intaris.config import WebhookConfig
from intaris.webhook import WebhookClient


def _make_config(
    url: str = "https://example.com/webhook",
    secret: str = "test-secret",
    timeout_ms: int = 3000,
    base_url: str = "",
) -> WebhookConfig:
    """Create a test WebhookConfig."""
    return WebhookConfig(
        url=url, secret=secret, timeout_ms=timeout_ms, base_url=base_url
    )


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


class TestWebhookClient:
    """Tests for WebhookClient."""

    def test_is_configured_with_url(self):
        """is_configured returns True when URL is set."""
        client = WebhookClient(_make_config(url="https://example.com"))
        assert client.is_configured() is True

    def test_is_configured_without_url(self):
        """is_configured returns False when URL is empty."""
        client = WebhookClient(_make_config(url=""))
        assert client.is_configured() is False

    def test_sign(self):
        """HMAC-SHA256 signature is computed correctly."""
        client = WebhookClient(_make_config(secret="my-secret"))
        body = b'{"test": true}'
        sig = client._sign(body)
        expected = hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
        assert sig == expected

    def test_successful_delivery(self):
        """Webhook is delivered successfully."""

        async def run():
            config = _make_config()
            client = WebhookClient(config)
            transport = httpx.MockTransport(lambda request: httpx.Response(200))
            client._client = httpx.AsyncClient(transport=transport)

            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id="agent-1",
                tool="bash",
                args_redacted={"command": "rm -rf /tmp/test"},
                risk="high",
                reasoning="Dangerous command",
            )
            await client.close()

        _run(run())

    def test_delivery_with_intaris_url(self):
        """Webhook payload includes intaris_url when base_url is configured."""

        async def run():
            config = _make_config(base_url="https://intaris.example.com")
            client = WebhookClient(config)
            captured_body = None

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal captured_body
                captured_body = json.loads(request.content)
                return httpx.Response(200)

            transport = httpx.MockTransport(handler)
            client._client = httpx.AsyncClient(transport=transport)

            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id=None,
                tool="bash",
                args_redacted={},
                risk="high",
                reasoning="Test",
            )

            assert captured_body is not None
            assert captured_body["intaris_url"] == (
                "https://intaris.example.com/ui/audit/call-1"
            )
            await client.close()

        _run(run())

    def test_delivery_signature(self):
        """Webhook request includes correct HMAC signature."""

        async def run():
            config = _make_config(secret="sig-test-secret")
            client = WebhookClient(config)
            captured_headers = None
            captured_body = None

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal captured_headers, captured_body
                captured_headers = dict(request.headers)
                captured_body = request.content
                return httpx.Response(200)

            transport = httpx.MockTransport(handler)
            client._client = httpx.AsyncClient(transport=transport)

            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id=None,
                tool="bash",
                args_redacted={},
                risk="high",
                reasoning="Test",
            )

            assert captured_headers is not None
            assert "x-intaris-signature" in captured_headers
            expected_sig = hmac.new(
                b"sig-test-secret", captured_body, hashlib.sha256
            ).hexdigest()
            assert captured_headers["x-intaris-signature"] == f"sha256={expected_sig}"
            await client.close()

        _run(run())

    def test_retry_on_failure(self):
        """Webhook retries once on failure."""

        async def run():
            config = _make_config()
            client = WebhookClient(config)
            call_count = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return httpx.Response(500)
                return httpx.Response(200)

            transport = httpx.MockTransport(handler)
            client._client = httpx.AsyncClient(transport=transport)

            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id=None,
                tool="bash",
                args_redacted={},
                risk="high",
                reasoning="Test",
            )
            assert call_count == 2
            await client.close()

        _run(run())

    def test_fire_and_forget(self):
        """Webhook errors do not propagate."""

        async def run():
            config = _make_config()
            client = WebhookClient(config)
            transport = httpx.MockTransport(lambda request: httpx.Response(500))
            client._client = httpx.AsyncClient(transport=transport)

            # Should not raise even when both attempts fail
            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id=None,
                tool="bash",
                args_redacted={},
                risk="high",
                reasoning="Test",
            )
            await client.close()

        _run(run())

    def test_no_delivery_when_not_configured(self):
        """No delivery attempt when URL is empty."""

        async def run():
            config = _make_config(url="")
            client = WebhookClient(config)

            await client.send_escalation(
                call_id="call-1",
                session_id="sess-1",
                user_id="user-1",
                agent_id=None,
                tool="bash",
                args_redacted={},
                risk="high",
                reasoning="Test",
            )
            assert client._client is None

        _run(run())

    def test_close_without_client(self):
        """close() is safe when no client was created."""

        async def run():
            config = _make_config(url="")
            client = WebhookClient(config)
            await client.close()

        _run(run())

    def test_payload_structure(self):
        """Webhook payload has the expected structure."""

        async def run():
            config = _make_config()
            client = WebhookClient(config)
            captured_body = None

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal captured_body
                captured_body = json.loads(request.content)
                return httpx.Response(200)

            transport = httpx.MockTransport(handler)
            client._client = httpx.AsyncClient(transport=transport)

            await client.send_escalation(
                call_id="call-123",
                session_id="sess-456",
                user_id="user-789",
                agent_id="agent-abc",
                tool="bash",
                args_redacted={"command": "test"},
                risk="high",
                reasoning="Test reasoning",
            )

            assert captured_body is not None
            assert captured_body["type"] == "escalation"
            assert captured_body["call_id"] == "call-123"
            assert captured_body["session_id"] == "sess-456"
            assert captured_body["user_id"] == "user-789"
            assert captured_body["agent_id"] == "agent-abc"
            assert captured_body["tool"] == "bash"
            assert captured_body["args_redacted"] == {"command": "test"}
            assert captured_body["risk"] == "high"
            assert captured_body["reasoning"] == "Test reasoning"
            assert "timestamp" in captured_body
            await client.close()

        _run(run())
