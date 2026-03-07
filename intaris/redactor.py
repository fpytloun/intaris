"""Secret redaction for audit log arguments.

Redacts sensitive values from tool call arguments before they are
persisted in the audit log. Never mutates the input — always returns
a deep copy with secrets replaced by [REDACTED:<type>] markers.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# ── Redaction Patterns ────────────────────────────────────────────────
# Each pattern is a tuple of (compiled regex, redaction type label).
# Patterns are applied to string values in the args dict.

_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI API keys
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "api_key"),
    # AWS access keys
    (re.compile(r"AKIA[A-Z0-9]{16}"), "aws_key"),
    # AWS secret keys (40 chars, base64-like)
    (
        re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"),
        "aws_secret",
    ),
    # GitHub tokens
    (re.compile(r"gh[ps]_[a-zA-Z0-9]{36,}"), "github_token"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{22,}"), "github_token"),
    # GitLab tokens
    (re.compile(r"glpat-[a-zA-Z0-9_-]{20,}"), "gitlab_token"),
    # Slack tokens
    (re.compile(r"xox[bpras]-[a-zA-Z0-9-]+"), "slack_token"),
    # Generic bearer tokens
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "bearer_token"),
    # Connection strings
    (
        re.compile(
            r"(?:postgresql|postgres|mysql|mongodb|redis|amqp|amqps)://[^\s\"']+"
        ),
        "connection_string",
    ),
    # Generic API key patterns in key=value format
    (
        re.compile(
            r"(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret[_-]?key|private[_-]?key)=\S+",
            re.IGNORECASE,
        ),
        "credential",
    ),
    # Password in key=value format
    (re.compile(r"(?:password|passwd|pwd)=\S+", re.IGNORECASE), "password"),
    # JWT tokens (three base64 segments separated by dots)
    (re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"), "jwt"),
    # Private key blocks
    (
        re.compile(
            r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----"
        ),
        "private_key",
    ),
]

# Keys in args dicts that likely contain secrets (case-insensitive match).
_SENSITIVE_KEYS: set[str] = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "secret_key",
    "private_key",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "connection_string",
    "dsn",
}


def redact(args: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from tool call arguments.

    Returns a deep copy of args with sensitive values replaced by
    [REDACTED:<type>] markers. The original args dict is never modified.

    Args:
        args: Tool call arguments (may be nested dicts/lists).

    Returns:
        Deep copy with secrets redacted.
    """
    return _redact_value(copy.deepcopy(args))


def _redact_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact secrets from a value."""
    if isinstance(value, dict):
        return {k: _redact_value(v, key=k) for k, v in value.items()}

    if isinstance(value, list):
        return [_redact_value(item) for item in value]

    if isinstance(value, str):
        return _redact_string(value, key=key)

    return value


def _redact_string(text: str, key: str | None = None) -> str:
    """Redact secrets from a string value.

    If the key name suggests a secret, redact the entire value.
    Otherwise, apply pattern-based redaction to the content.
    """
    # If the key name suggests a secret, redact the entire value
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED:credential]"

    # Apply pattern-based redaction
    result = text
    for pattern, label in _VALUE_PATTERNS:
        result = pattern.sub(f"[REDACTED:{label}]", result)

    return result
