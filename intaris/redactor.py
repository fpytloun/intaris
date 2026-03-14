"""Secret redaction for audit log arguments.

Redacts sensitive values from tool call arguments before they are
persisted in the audit log. Never mutates the input — always returns
a deep copy with secrets replaced by [REDACTED:<type>] markers.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

# ── Validators ────────────────────────────────────────────────────────
# Optional post-match validators for patterns prone to false positives.
# Each receives the Match object and returns True if it's a real secret.


def _is_likely_api_key(match: re.Match[str]) -> bool:
    """Reject branch names and identifiers that happen to start with sk-.

    Real OpenAI keys contain mixed case and digits in the random portion.
    Branch names like ``sk-cleanup-old-sessions`` are lowercase + hyphens.
    """
    suffix = match.group()[3:]  # after "sk-"
    has_upper = any(c.isupper() for c in suffix)
    has_digit = any(c.isdigit() for c in suffix)
    return has_upper and has_digit


def _is_likely_aws_secret(match: re.Match[str]) -> bool:
    """Reject file paths, SHA-1 hashes, and other 40-char non-secrets.

    Real AWS secrets are base64-encoded random bytes — they contain a mix
    of uppercase, lowercase, and digits with very few slashes (0-2 typical).
    File paths have many slashes and start with ``/``. SHA-1 hashes are
    lowercase hex only.
    """
    text = match.group()
    # File paths: many slashes or starts with /
    if text.count("/") > 3:
        return False
    if text.startswith("/"):
        return False
    # SHA-1 hashes: only lowercase hex chars (a-f + digits)
    if all(c in "0123456789abcdef" for c in text):
        return False
    # Require mixed character classes (upper + lower + digit)
    has_upper = any(c.isupper() for c in text)
    has_lower = any(c.islower() for c in text)
    has_digit = any(c.isdigit() for c in text)
    return has_upper and has_lower and has_digit


# ── Redaction Patterns ────────────────────────────────────────────────
# Each entry is (regex, label) or (regex, label, validator).
# When a validator is present, the match is only redacted if the
# validator returns True — this eliminates false positives.

_PatternEntry = (
    tuple[re.Pattern[str], str]
    | tuple[re.Pattern[str], str, Callable[[re.Match[str]], bool]]
)

_VALUE_PATTERNS: list[_PatternEntry] = [
    # OpenAI API keys — validator rejects branch names (lowercase-only)
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "api_key", _is_likely_api_key),
    # AWS access keys
    (re.compile(r"AKIA[A-Z0-9]{16}"), "aws_key"),
    # AWS secret keys (40 chars, base64-like) — validator rejects paths/hashes
    (
        re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"),
        "aws_secret",
        _is_likely_aws_secret,
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
    # Word boundary prevents matching inside OPENAI_API_KEY=, validate_api_key=
    # Constrained value class + min 8 chars filters out code assignments
    (
        re.compile(
            r"(?<![a-zA-Z0-9_])"
            r"(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|secret[_-]?key|private[_-]?key)"
            r"=[\"']?"
            r"[a-zA-Z0-9_\-/+]{8,}",
            re.IGNORECASE,
        ),
        "credential",
    ),
    # Password in key=value format
    # Removed "pwd" — too many false positives with shell $PWD variable.
    # _SENSITIVE_KEYS still catches {"pwd": "secret"} via key-name matching.
    # Word boundary prevents matching inside change_password=, reset_password=
    (
        re.compile(
            r"(?<![a-zA-Z0-9_])"
            r"(?:password|passwd)"
            r"=[\"']?"
            r"[a-zA-Z0-9_\-/+]{8,}",
            re.IGNORECASE,
        ),
        "password",
    ),
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
    for entry in _VALUE_PATTERNS:
        pattern = entry[0]
        label = entry[1]
        validator = entry[2] if len(entry) > 2 else None

        if validator:
            _v = validator  # capture for closure
            _l = label

            def _replacer(
                m: re.Match[str],
                *,
                v: Callable[[re.Match[str]], bool] = _v,
                lbl: str = _l,
            ) -> str:
                return f"[REDACTED:{lbl}]" if v(m) else m.group()

            result = pattern.sub(_replacer, result)
        else:
            result = pattern.sub(f"[REDACTED:{label}]", result)

    return result
