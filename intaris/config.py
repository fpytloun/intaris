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


def _parse_api_keys() -> dict[str, str]:
    """Parse INTARIS_API_KEYS env var into a dict mapping key → user_id.

    Format: JSON object {"api-key-1": "username", "api-key-2": "*"}
    A value of "*" means the key authenticates but does not bind to a user_id.
    """
    import json

    raw = os.environ.get("INTARIS_API_KEYS", "")
    if not raw:
        return {}
    try:
        keys = json.loads(raw)
        if not isinstance(keys, dict):
            raise ValueError("INTARIS_API_KEYS must be a JSON object")
        return {str(k): str(v) for k, v in keys.items()}
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse INTARIS_API_KEYS: %s at position %d", e.msg, e.pos or 0
        )
        return {}
    except ValueError as e:
        logger.error("Failed to parse INTARIS_API_KEYS: invalid value")
        return {}


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

    model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-5-nano"))
    base_url: str = field(default_factory=_llm_base_url)
    api_key: str = field(default_factory=_llm_api_key)
    temperature: float = 0.1
    reasoning_effort: str | None = field(
        default_factory=lambda: _env("LLM_REASONING_EFFORT", "low") or None
    )
    # Timeout in milliseconds for LLM evaluation calls.
    # Must be well under the 5-second circuit breaker in the Executor Adapter.
    timeout_ms: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_MS", 4000))


@dataclass
class DBConfig:
    """Database configuration.

    Supports SQLite (dev) and PostgreSQL (prod) backends. The backend
    is selected via DB_BACKEND env var. SQLite is the default for local
    development — just run ``intaris`` with no database config.

    For PostgreSQL, set DB_BACKEND=postgresql and DATABASE_URL to a
    connection string like ``postgresql://user:pass@host:5432/intaris``.
    """

    # Backend: "sqlite" (default) or "postgresql".
    backend: str = field(default_factory=lambda: _env("DB_BACKEND", "sqlite"))

    # SQLite: plain path for database file.
    path: str = field(
        default_factory=lambda: (
            _env("DB_PATH") or os.path.join(_data_dir(), "intaris.db")
        )
    )

    # PostgreSQL: connection URL.
    # Format: postgresql://user:password@host:port/dbname
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))


@dataclass
class ServerConfig:
    """HTTP server configuration."""

    host: str = field(default_factory=lambda: _env("INTARIS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("INTARIS_PORT", 8060))

    # Single shared API key (authenticates but does not bind to user_id).
    api_key: str = field(default_factory=lambda: _env("INTARIS_API_KEY"))

    # Multi-key with user_id mapping: {"key": "username", "key2": "*"}
    # A value of "*" means auth-only (no user binding).
    api_keys: dict[str, str] = field(default_factory=_parse_api_keys)

    # Max evaluations per session per minute (0 = no limit).
    rate_limit: int = field(default_factory=lambda: _env_int("RATE_LIMIT", 60))


@dataclass
class WebhookConfig:
    """Webhook callback configuration (for Cognis integration).

    When configured, Intaris sends an HMAC-signed HTTP POST to the webhook
    URL on every escalation decision. This enables Cognis (or any external
    system) to populate an approval queue.
    """

    url: str = field(default_factory=lambda: _env("WEBHOOK_URL"))
    secret: str = field(default_factory=lambda: _env("WEBHOOK_SECRET"))
    timeout_ms: int = field(
        default_factory=lambda: _env_int("WEBHOOK_TIMEOUT_MS", 3000)
    )
    # Base URL for constructing intaris_url in webhook payloads.
    # If unset, derived from the request Host header (less secure).
    base_url: str = field(default_factory=lambda: _env("INTARIS_BASE_URL"))


@dataclass
class MCPConfig:
    """MCP proxy configuration.

    Controls the MCP proxy feature: file-based server config,
    stdio transport gating, encryption for secrets at rest,
    and upstream call timeouts.
    """

    # Path to multi-user MCP config JSON file (optional).
    config_file: str = field(default_factory=lambda: _env("MCP_CONFIG_FILE"))

    # Allow stdio transport for MCP servers (disable in multi-tenant).
    allow_stdio: bool = field(
        default_factory=lambda: _env_bool("MCP_ALLOW_STDIO", default=True)
    )

    # Fernet key for encrypting secrets at rest (env vars, HTTP headers).
    # Required when MCP servers have secrets. Generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = field(default_factory=lambda: _env("INTARIS_ENCRYPTION_KEY"))

    # Timeout in milliseconds for upstream MCP server calls.
    upstream_timeout_ms: int = field(
        default_factory=lambda: _env_int("MCP_UPSTREAM_TIMEOUT_MS", 30000)
    )

    # Base directory for MCP server package caches (npx, uvx).
    # Per-server subdirectories are created automatically to isolate
    # concurrent installs and prevent cache corruption.
    cache_dir: str = field(
        default_factory=lambda: (
            _env("MCP_CACHE_DIR") or os.path.join(_data_dir(), "mcp-cache")
        )
    )


@dataclass
class AnalysisConfig:
    """Behavioral analysis configuration.

    Controls the behavioral guardrails feature: session summaries,
    cross-session analysis, and behavioral profiling.
    """

    # Master switch for behavioral analysis.
    enabled: bool = field(
        default_factory=lambda: _env_bool("ANALYSIS_ENABLED", default=True)
    )

    # Minutes of inactivity before a session transitions to idle.
    session_idle_timeout_min: int = field(
        default_factory=lambda: _env_int("SESSION_IDLE_TIMEOUT_MINUTES", 30)
    )

    # Number of evaluate calls per session before triggering a summary.
    summary_volume_threshold: int = field(
        default_factory=lambda: _env_int("SUMMARY_VOLUME_THRESHOLD", 50)
    )

    # Minutes between periodic cross-session analysis runs.
    analysis_interval_min: int = field(
        default_factory=lambda: _env_int("ANALYSIS_INTERVAL_MINUTES", 60)
    )

    # Days of history to include in cross-session analysis.
    # 7-day window balances pattern detection with responsive risk decay —
    # incidents older than 7 days age out of the active profile while
    # historical analyses remain permanently stored for visibility.
    lookback_days: int = field(
        default_factory=lambda: _env_int("ANALYSIS_LOOKBACK_DAYS", 7)
    )

    # Number of parallel task queue workers.
    worker_count: int = field(
        default_factory=lambda: _env_int("ANALYSIS_WORKER_COUNT", 4)
    )

    # Note: ANALYSIS_WINDOW_CHARS and ANALYSIS_L3_WINDOW_CHARS are read
    # directly by the analyzer module at import time (module-level
    # constants) rather than through this config object.  This avoids
    # circular imports and ensures the budget is available to all
    # partitioner functions without threading config through every call.


@dataclass
class NotificationConfig:
    """Per-user notification system configuration.

    Controls the action token TTL for one-click approve/deny links
    in notification messages. Channel configuration is per-user in
    the database, not in environment variables.
    """

    # Minutes before action tokens expire (approve/deny links).
    action_ttl_minutes: int = field(
        default_factory=lambda: _env_int("NOTIFICATION_ACTION_TTL_MINUTES", 60)
    )


@dataclass
class JudgeConfig:
    """Judge auto-resolution configuration.

    Controls the judge feature: a more capable LLM that automatically
    reviews escalated tool calls and resolves them without requiring
    human intervention.

    Modes:
    - ``disabled``: No judge. Escalations require human resolution.
    - ``auto``: Judge auto-resolves: approve or deny. Deny if uncertain.
    - ``advisory``: Judge reviews: approve, deny, or defer to human.

    Notification modes (when judge is enabled):
    - ``deny_only``: Only notify when judge denies (default).
    - ``always``: Notify on escalation (before judge) AND on resolution.
    - ``never``: Fully silent — no notifications in judge mode.
    """

    # Judge mode: disabled, auto, advisory.
    mode: str = field(default_factory=lambda: _env("JUDGE_MODE", "disabled"))

    # Notification mode when judge is enabled: deny_only, always, never.
    notify_mode: str = field(
        default_factory=lambda: _env("JUDGE_NOTIFY_MODE", "deny_only")
    )


@dataclass
class EventStoreConfig:
    """Session event store configuration.

    Controls the session recording feature: append-only event logs
    that capture the full session timeline for live tailing, playback,
    reconstruction, and behavioral analysis.

    Storage uses chunked ndjson files — one chunk per flush. Both
    filesystem and S3 backends use the same chunked layout:
      {user_id}/{session_id}/seq_{start:06d}_{end:06d}.ndjson
    """

    # Master switch for the event store.
    enabled: bool = field(
        default_factory=lambda: _env_bool("EVENT_STORE_ENABLED", default=True)
    )

    # Storage backend: "filesystem" or "s3".
    backend: str = field(
        default_factory=lambda: _env("EVENT_STORE_BACKEND", "filesystem")
    )

    # Filesystem settings (default for local development).
    filesystem_path: str = field(
        default_factory=lambda: (
            _env("EVENT_STORE_PATH") or os.path.join(_data_dir(), "events")
        )
    )

    # S3 / MinIO settings.
    s3_endpoint: str = field(
        default_factory=lambda: _env("EVENT_STORE_S3_ENDPOINT", "http://localhost:9000")
    )
    s3_access_key: str = field(
        default_factory=lambda: _env("EVENT_STORE_S3_ACCESS_KEY")
    )
    s3_secret_key: str = field(
        default_factory=lambda: _env("EVENT_STORE_S3_SECRET_KEY")
    )
    s3_bucket: str = field(
        default_factory=lambda: _env("EVENT_STORE_S3_BUCKET", "intaris-events")
    )
    s3_region: str = field(default_factory=lambda: _env("EVENT_STORE_S3_REGION"))

    # Write buffer: max events per chunk before flushing.
    flush_size: int = field(
        default_factory=lambda: _env_int("EVENT_STORE_FLUSH_SIZE", 100)
    )

    # Write buffer: seconds between periodic flushes.
    flush_interval: int = field(
        default_factory=lambda: _env_int("EVENT_STORE_FLUSH_INTERVAL", 30)
    )


def _build_analysis_llm_config() -> LLMConfig:
    """Build LLM config for L2 analysis tasks (session summaries).

    Reads ANALYSIS_LLM_* env vars with fallback to the evaluate LLM
    values for base_url and api_key (same provider/key is the common
    case). Model, reasoning effort, and timeout must be explicitly set
    or use analysis-specific defaults.
    """
    return LLMConfig(
        model=_env("ANALYSIS_LLM_MODEL", "gpt-5-mini"),
        base_url=_env("ANALYSIS_LLM_BASE_URL") or _llm_base_url(),
        api_key=_env("ANALYSIS_LLM_API_KEY") or _llm_api_key(),
        temperature=0.1,
        reasoning_effort=_env("ANALYSIS_LLM_REASONING_EFFORT", "low") or None,
        timeout_ms=_env_int("ANALYSIS_LLM_TIMEOUT_MS", 30000),
    )


def _build_l3_analysis_llm_config() -> LLMConfig:
    """Build LLM config for L3 analysis tasks (cross-session behavioral).

    L3 analysis detects subtle cross-session patterns (progressive
    escalation, coordinated access, intent masking) that require a
    more capable model than L2 session summaries.

    Reads ANALYSIS_L3_LLM_* env vars with fallback to the ANALYSIS_LLM_*
    values, then to the evaluate LLM values for base_url and api_key.
    """
    return LLMConfig(
        model=_env("ANALYSIS_L3_LLM_MODEL") or _env("ANALYSIS_LLM_MODEL", "gpt-5.4"),
        base_url=_env("ANALYSIS_L3_LLM_BASE_URL")
        or _env("ANALYSIS_LLM_BASE_URL")
        or _llm_base_url(),
        api_key=_env("ANALYSIS_L3_LLM_API_KEY")
        or _env("ANALYSIS_LLM_API_KEY")
        or _llm_api_key(),
        temperature=0.1,
        reasoning_effort=_env("ANALYSIS_L3_LLM_REASONING_EFFORT")
        or _env("ANALYSIS_LLM_REASONING_EFFORT", "low")
        or None,
        timeout_ms=_env_int(
            "ANALYSIS_L3_LLM_TIMEOUT_MS",
            _env_int("ANALYSIS_LLM_TIMEOUT_MS", 30000),
        ),
    )


def _build_judge_llm_config() -> LLMConfig:
    """Build LLM config for judge auto-resolution.

    The judge uses a more capable model (default gpt-5.4) with longer
    timeout than the evaluate model. It reviews escalated tool calls
    with richer session context and makes approve/deny decisions.

    Reads JUDGE_LLM_* env vars with fallback to the evaluate LLM
    values for base_url and api_key (same provider/key is the common
    case).
    """
    return LLMConfig(
        model=_env("JUDGE_LLM_MODEL", "gpt-5.4"),
        base_url=_env("JUDGE_LLM_BASE_URL") or _llm_base_url(),
        api_key=_env("JUDGE_LLM_API_KEY") or _llm_api_key(),
        temperature=0.1,
        reasoning_effort=_env("JUDGE_LLM_REASONING_EFFORT", "low") or None,
        timeout_ms=_env_int("JUDGE_LLM_TIMEOUT_MS", 15000),
    )


@dataclass
class Config:
    """Root configuration container."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_analysis: LLMConfig = field(default_factory=_build_analysis_llm_config)
    llm_l3_analysis: LLMConfig = field(default_factory=_build_l3_analysis_llm_config)
    llm_judge: LLMConfig = field(default_factory=_build_judge_llm_config)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    db: DBConfig = field(default_factory=DBConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    event_store: EventStoreConfig = field(default_factory=EventStoreConfig)

    def validate(self) -> None:
        """Validate that required configuration is present."""
        # Database backend validation.
        if self.db.backend not in ("sqlite", "postgresql"):
            raise ValueError(
                f"DB_BACKEND={self.db.backend} is not supported. "
                "Use 'sqlite' or 'postgresql'."
            )
        if self.db.backend == "postgresql" and not self.db.database_url:
            raise ValueError(
                "DATABASE_URL is required when DB_BACKEND=postgresql. "
                "Format: postgresql://user:password@host:port/dbname"
            )

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

        # Fail loudly if INTARIS_API_KEYS env var is set but parsed as empty
        # (indicates malformed JSON that was silently ignored at parse time).
        raw_keys = os.environ.get("INTARIS_API_KEYS", "")
        if raw_keys and not self.server.api_keys:
            raise ValueError(
                "INTARIS_API_KEYS is set but could not be parsed. "
                'Must be a JSON object: {"key": "username", ...}'
            )

        # Webhook secret is required when webhook URL is configured.
        if self.webhook.url and not self.webhook.secret:
            raise ValueError(
                "WEBHOOK_SECRET is required when WEBHOOK_URL is set. "
                "Unsigned webhooks are not allowed."
            )

        # Validate encryption key format if provided.
        if self.mcp.encryption_key:
            from intaris.crypto import validate_key

            if not validate_key(self.mcp.encryption_key):
                raise ValueError(
                    "INTARIS_ENCRYPTION_KEY is not a valid Fernet key. "
                    "Generate one with: python -c "
                    '"from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                )

        # Validate config file exists if specified.
        if self.mcp.config_file and not os.path.isfile(self.mcp.config_file):
            raise ValueError(f"MCP_CONFIG_FILE={self.mcp.config_file} does not exist.")

        # Analysis LLM is required when behavioral analysis is enabled.
        # No fallback from llm_analysis to llm — prevents silent
        # misconfiguration where a fast/cheap model produces garbage
        # for analysis tasks that need a more capable model.
        if self.analysis.enabled and not self.llm_analysis.api_key:
            raise ValueError(
                "ANALYSIS_LLM_API_KEY (or LLM_API_KEY as fallback) is required "
                "when ANALYSIS_ENABLED=true. Analysis requires a separate LLM "
                "configuration (typically a more capable model with longer "
                "timeout than the evaluate model). Set ANALYSIS_ENABLED=false "
                "to disable behavioral analysis."
            )

        # Judge configuration validation.
        if self.judge.mode not in ("disabled", "auto", "advisory"):
            raise ValueError(
                f"JUDGE_MODE={self.judge.mode} is not supported. "
                "Use 'disabled', 'auto', or 'advisory'."
            )
        if self.judge.notify_mode not in ("deny_only", "always", "never"):
            raise ValueError(
                f"JUDGE_NOTIFY_MODE={self.judge.notify_mode} is not supported. "
                "Use 'deny_only', 'always', or 'never'."
            )
        if self.judge.mode != "disabled" and not self.llm_judge.api_key:
            logger.warning(
                "JUDGE_MODE=%s but no judge LLM API key configured. "
                "Judge will fail on first review. Set JUDGE_LLM_API_KEY "
                "or LLM_API_KEY.",
                self.judge.mode,
            )

        # Event store backend validation.
        if self.event_store.enabled:
            if self.event_store.backend not in ("filesystem", "s3"):
                raise ValueError(
                    f"EVENT_STORE_BACKEND={self.event_store.backend} is not "
                    "supported. Use 'filesystem' or 's3'."
                )
            if self.event_store.backend == "s3":
                if (
                    not self.event_store.s3_access_key
                    or not self.event_store.s3_secret_key
                ):
                    raise ValueError(
                        "EVENT_STORE_S3_ACCESS_KEY and EVENT_STORE_S3_SECRET_KEY "
                        "are required when EVENT_STORE_BACKEND=s3."
                    )
            if self.event_store.flush_size < 1:
                raise ValueError(
                    f"EVENT_STORE_FLUSH_SIZE={self.event_store.flush_size} "
                    "must be >= 1."
                )
            if self.event_store.flush_interval < 1:
                raise ValueError(
                    f"EVENT_STORE_FLUSH_INTERVAL={self.event_store.flush_interval} "
                    "must be >= 1."
                )


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
