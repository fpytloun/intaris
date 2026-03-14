# Configuration Reference

All settings are loaded from environment variables. Defaults are optimized for local development -- just set `LLM_API_KEY` and run.

Data is stored in `~/.intaris` by default (override with `DATA_DIR`). In Docker, `DATA_DIR` is set to `/data` for volume mounting.

## Server

| Variable | Default | Description |
|---|---|---|
| `INTARIS_HOST` | `0.0.0.0` | HTTP server bind address |
| `INTARIS_PORT` | `8060` | HTTP server port |
| `INTARIS_API_KEY` | (empty) | Single shared API key. Authenticates requests but does not bind to a user -- clients must send `X-User-Id` header. |
| `INTARIS_API_KEYS` | (empty) | JSON dict mapping API keys to user IDs: `{"key1": "alice", "key2": "bob", "key3": "*"}`. A value of `"*"` means auth-only (no user binding). |
| `RATE_LIMIT` | `60` | Max evaluations per session per minute. Set to `0` to disable. |
| `DATA_DIR` | `~/.intaris` | Base directory for database and event store files |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Authentication

At least one of `INTARIS_API_KEY` or `INTARIS_API_KEYS` should be set in production. Without either, the server accepts unauthenticated requests.

With `INTARIS_API_KEY` (single shared key), clients must also send `X-User-Id` to identify themselves. With `INTARIS_API_KEYS`, the user identity is resolved from the key mapping.

Clients authenticate via `Authorization: Bearer <key>` header or `X-API-Key: <key>` header.

## LLM (Safety Evaluation)

| Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | (required) | API key for the LLM provider. Falls back to `OPENAI_API_KEY`. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API base URL. Falls back to `OPENAI_API_BASE`. |
| `LLM_MODEL` | `gpt-5-nano` | Model for safety evaluation. Should be fast and cheap. |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort hint (provider-specific). |
| `LLM_TIMEOUT_MS` | `4000` | Timeout for LLM calls in milliseconds. Must be under the 5-second circuit breaker. Minimum: 500ms. |

### Model Selection

The evaluation model should be fast and inexpensive -- it's called on every non-read-only tool call. `gpt-5-nano` or similar small models work well. The model must support structured output (JSON mode).

## LLM (Behavioral Analysis)

Separate LLM configuration for analysis tasks (session summaries, cross-session analysis). Typically a more capable model with longer timeout.

| Variable | Default | Description |
|---|---|---|
| `ANALYSIS_LLM_API_KEY` | (falls back to `LLM_API_KEY`) | API key for analysis LLM |
| `ANALYSIS_LLM_BASE_URL` | (falls back to `LLM_BASE_URL`) | Base URL for analysis LLM |
| `ANALYSIS_LLM_MODEL` | `gpt-5-mini` | Model for analysis tasks |
| `ANALYSIS_LLM_REASONING_EFFORT` | `low` | Reasoning effort for analysis |
| `ANALYSIS_LLM_TIMEOUT_MS` | `30000` | Timeout for analysis LLM calls (30s) |

## Database

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `~/.intaris/intaris.db` | SQLite database file path |

## Behavioral Analysis

| Variable | Default | Description |
|---|---|---|
| `ANALYSIS_ENABLED` | `true` | Master switch for behavioral analysis pipeline |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `30` | Minutes of inactivity before session transitions to idle |
| `SUMMARY_VOLUME_THRESHOLD` | `50` | Evaluate calls per session before triggering a summary |
| `ANALYSIS_INTERVAL_MINUTES` | `60` | Minutes between periodic cross-session analysis runs |
| `ANALYSIS_LOOKBACK_DAYS` | `30` | Days of history to include in cross-session analysis |

## Barriers

| Variable | Default | Description |
|---|---|---|
| `INTENTION_BARRIER_TIMEOUT_MS` | `1000` | Max time (ms) the evaluate endpoint waits for a pending intention update |
| `ALIGNMENT_BARRIER_TIMEOUT_MS` | `15000` | Max time (ms) the evaluate endpoint waits for a pending alignment check |

## Webhook

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_URL` | (empty) | URL for escalation webhook callbacks (optional) |
| `WEBHOOK_SECRET` | (empty) | HMAC-SHA256 secret for signing webhook payloads. Required if `WEBHOOK_URL` is set. |
| `WEBHOOK_TIMEOUT_MS` | `3000` | Webhook HTTP timeout in milliseconds |
| `INTARIS_BASE_URL` | (empty) | Base URL for constructing `intaris_url` in webhook payloads |

## MCP Proxy

| Variable | Default | Description |
|---|---|---|
| `MCP_CONFIG_FILE` | (empty) | Path to JSON file defining MCP servers (optional) |
| `MCP_ALLOW_STDIO` | `true` | Allow stdio transport for MCP servers. Disable in multi-tenant deployments. |
| `INTARIS_ENCRYPTION_KEY` | (empty) | Fernet key for encrypting MCP server secrets at rest. Required when servers have secrets. |
| `MCP_UPSTREAM_TIMEOUT_MS` | `30000` | Timeout for upstream MCP server calls in milliseconds |

### Generating an Encryption Key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Event Store (Session Recording)

| Variable | Default | Description |
|---|---|---|
| `EVENT_STORE_ENABLED` | `true` | Master switch for session recording |
| `EVENT_STORE_BACKEND` | `filesystem` | Storage backend: `filesystem` or `s3` |
| `EVENT_STORE_PATH` | `~/.intaris/events` | Filesystem backend storage path |
| `EVENT_STORE_FLUSH_SIZE` | `100` | Events per chunk before flushing |
| `EVENT_STORE_FLUSH_INTERVAL` | `30` | Seconds between periodic flushes |

### S3 Backend

| Variable | Default | Description |
|---|---|---|
| `EVENT_STORE_S3_ENDPOINT` | `http://localhost:9000` | S3/MinIO endpoint URL |
| `EVENT_STORE_S3_ACCESS_KEY` | (required for S3) | S3 access key |
| `EVENT_STORE_S3_SECRET_KEY` | (required for S3) | S3 secret key |
| `EVENT_STORE_S3_BUCKET` | `intaris-events` | S3 bucket name |
| `EVENT_STORE_S3_REGION` | (empty) | S3 region |

## Notifications

| Variable | Default | Description |
|---|---|---|
| `NOTIFICATION_ACTION_TTL_MINUTES` | `60` | TTL for one-click approve/deny action tokens in minutes |

Notification channels (Pushover, Slack, webhook) are configured per-user via the REST API, not environment variables.

## Client-Side Variables

These are set on the client machine, not the Intaris server:

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication |
| `INTARIS_AGENT_ID` | `opencode` / `claude-code` | Agent identifier |
| `INTARIS_USER_ID` | (empty) | User identifier (required with single-key auth) |
| `INTARIS_FAIL_OPEN` | `false` | Allow tool calls when Intaris is unreachable |
| `INTARIS_INTENTION` | (auto) | Session intention override |
| `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent dirs for cross-project reads |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between periodic checkpoints (0 = disabled) |
| `INTARIS_SESSION_RECORDING` | `false` | Enable session recording (Claude Code only) |
| `INTARIS_DEBUG` | `false` | Enable debug logging (Claude Code only) |
