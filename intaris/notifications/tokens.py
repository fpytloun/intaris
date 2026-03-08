"""Fernet-based signed action tokens for one-click approve/deny.

Generates short-lived, encrypted tokens that encode a specific action
(approve/deny) for a specific escalation (call_id + user_id). Tokens
are URL-safe and include an embedded timestamp for TTL verification.

Security properties:
- Encrypted (Fernet = AES-128-CBC + HMAC-SHA256) — cannot be forged
- Time-limited (configurable TTL, default 60 minutes)
- Single-purpose — each token encodes exactly one action
- Idempotent — resolution is enforced by the audit store's atomic
  WHERE user_decision IS NULL guard, not by the token itself

CSRF protection:
- generate_csrf_token() creates a random token for the confirmation page
- verify_csrf_token() validates it on POST submission
- CSRF tokens are embedded in the action token itself (no server state)
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# CSRF token length in bytes (16 bytes = 32 hex chars)
_CSRF_TOKEN_BYTES = 16


@dataclass
class ActionPayload:
    """Decoded action token payload."""

    call_id: str
    user_id: str
    action: str  # "approve" or "deny"
    csrf_token: str


def generate_action_token(
    *,
    call_id: str,
    user_id: str,
    action: str,
    encryption_key: str,
) -> str:
    """Generate an encrypted action token.

    The token encodes the call_id, user_id, action, and a CSRF token.
    The CSRF token is embedded in the payload so it can be verified
    on POST without server-side state.

    Args:
        call_id: The escalated call to act on.
        user_id: The user who owns the escalation.
        action: "approve" or "deny".
        encryption_key: Fernet key for encryption.

    Returns:
        URL-safe base64 encoded Fernet token.

    Raises:
        ValueError: If action is invalid or key is bad.
    """
    if action not in ("approve", "deny"):
        raise ValueError(f"Invalid action '{action}'. Must be 'approve' or 'deny'.")

    csrf_token = secrets.token_hex(_CSRF_TOKEN_BYTES)

    payload = json.dumps(
        {
            "c": call_id,
            "u": user_id,
            "a": action,
            "t": csrf_token,
        },
        separators=(",", ":"),
    ).encode()

    try:
        f = Fernet(encryption_key.encode())
    except Exception as e:
        raise ValueError(f"Invalid encryption key: {e}") from e

    return f.encrypt(payload).decode()


def verify_action_token(
    token: str,
    *,
    encryption_key: str,
    ttl_seconds: int,
) -> ActionPayload:
    """Decrypt and verify an action token.

    Args:
        token: The Fernet token to verify.
        encryption_key: Fernet key for decryption.
        ttl_seconds: Maximum token age in seconds.

    Returns:
        Decoded ActionPayload with call_id, user_id, action, csrf_token.

    Raises:
        ValueError: If token is expired, invalid, or malformed.
    """
    try:
        f = Fernet(encryption_key.encode())
    except Exception as e:
        raise ValueError(f"Invalid encryption key: {e}") from e

    try:
        data = f.decrypt(token.encode(), ttl=ttl_seconds)
    except InvalidToken:
        raise ValueError("Action token is invalid or expired")

    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Malformed action token payload: {e}") from e

    # Validate required fields
    call_id = payload.get("c")
    user_id = payload.get("u")
    action = payload.get("a")
    csrf_token = payload.get("t")

    if not all([call_id, user_id, action, csrf_token]):
        raise ValueError("Action token missing required fields")

    if action not in ("approve", "deny"):
        raise ValueError(f"Invalid action in token: {action}")

    return ActionPayload(
        call_id=call_id,
        user_id=user_id,
        action=action,
        csrf_token=csrf_token,
    )


def generate_action_urls(
    *,
    call_id: str,
    user_id: str,
    base_url: str,
    encryption_key: str,
) -> tuple[str, str]:
    """Generate approve and deny action URLs.

    Args:
        call_id: The escalated call.
        user_id: The user who owns the escalation.
        base_url: Intaris base URL (e.g., "https://intaris.example.com").
        encryption_key: Fernet key for token encryption.

    Returns:
        Tuple of (approve_url, deny_url).
    """
    base = base_url.rstrip("/")

    approve_token = generate_action_token(
        call_id=call_id,
        user_id=user_id,
        action="approve",
        encryption_key=encryption_key,
    )
    deny_token = generate_action_token(
        call_id=call_id,
        user_id=user_id,
        action="deny",
        encryption_key=encryption_key,
    )

    return (
        f"{base}/api/v1/action/{approve_token}",
        f"{base}/api/v1/action/{deny_token}",
    )
