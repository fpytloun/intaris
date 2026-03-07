"""Configuration management for intaris.

All settings are loaded from environment variables with sensible defaults.
Defaults are optimized for local development — just set LLM_API_KEY and run.

Data is stored in ~/.intaris by default (override with DATA_DIR env var).
In Docker, DATA_DIR is set to /data for volume mounting.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("intaris")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


def _data_dir() -> str:
    """Resolve base data directory.

    Priority: DATA_DIR env var > ~/.intaris
    In Docker, the Dockerfile sets DATA_DIR=/data for volume mounting.
    Locally, defaults to ~/.intaris for clean, predictable storage.
    """
    raw = os.environ.get("DATA_DIR", "")
    if raw:
        return raw
    return os.path.join(os.path.expanduser("~"), ".intaris")


def _llm_api_key() -> str:
    """Resolve LLM API key with fallback chain.

    Priority: LLM_API_KEY > OPENAI_API_KEY
    """
    return _env("LLM_API_KEY") or _env("OPENAI_API_KEY")


def _llm_base_url() -> str:
    """Resolve LLM base URL with fallback chain.

    Priority: LLM_BASE_URL > OPENAI_API_BASE > default OpenAI URL
    """
    return (
        _env("LLM_BASE_URL") or _env("OPENAI_API_BASE") or "https://api.openai.com/v1"
    )


@dataclass
class LLMConfig:
    """LLM configuration for safety evaluation."""

    model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4.1-nano"))
    base_url: str = field(default_factory=_llm_base_url)
    api_key: str = field(default_factory=_llm_api_key)
    temperature: float = 0.1
    reasoning_effort: str | None = field(
        default_factory=lambda: _env("LLM_REASONING_EFFORT") or None
    )
    # Timeout in milliseconds for LLM evaluation calls.
    # Must be well under the 5-second circuit breaker in the Executor Adapter.
    timeout_ms: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_MS", 4000))


@dataclass
class DBConfig:
    """Database configuration.

    Uses a plain file path for SQLite (dev). PostgreSQL support (prod)
    will be added in a future phase.
    """

    # Plain path for SQLite database file.
    path: str = field(
        default_factory=lambda: (
            _env("DB_PATH") or os.path.join(_data_dir(), "intaris.db")
        )
    )


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str = field(default_factory=lambda: _env("INTARIS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("INTARIS_PORT", 8060))
    api_key: str = field(default_factory=lambda: _env("INTARIS_API_KEY"))

    # Max evaluations per session per minute (0 = no limit).
    rate_limit: int = field(default_factory=lambda: _env_int("RATE_LIMIT", 60))


@dataclass
class WebhookConfig:
    """Webhook callback configuration (for Cognis integration).

    Deferred to Week 2 — schema defined here for config completeness.
    """

    url: str = field(default_factory=lambda: _env("WEBHOOK_URL"))
    secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))


@dataclass
class Config:
    """Root configuration container."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    db: DBConfig = field(default_factory=DBConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)

    def validate(self) -> None:
        """Validate that required configuration is present."""
        if not self.llm.api_key:
            raise ValueError(
                "LLM API key is required. Set LLM_API_KEY or OPENAI_API_KEY."
            )
        if self.llm.timeout_ms < 500:
            raise ValueError(
                f"LLM_TIMEOUT_MS={self.llm.timeout_ms} is too low. "
                "Minimum 500ms for reliable LLM calls."
            )
        if self.server.rate_limit < 0:
            raise ValueError(f"RATE_LIMIT={self.server.rate_limit} must be >= 0.")


def load_config() -> Config:
    """Load and validate configuration from environment variables.

    Creates the data directory (~/.intaris by default) if it doesn't exist.
    """
    config = Config()
    config.validate()

    # Ensure data directory exists
    data_dir = _data_dir()
    os.makedirs(data_dir, exist_ok=True)
    logger.debug("Data directory: %s", data_dir)

    return config
