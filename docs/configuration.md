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
| `INTARIS_JWT_PUBLIC_KEY` | (empty) | Path to a Cognis ES256 public key PEM for JWT validation |
| `INTARIS_JWKS_URL` | (empty) | JWKS URL for Cognis-issued JWT validation (mutually exclusive with `INTARIS_JWT_PUBLIC_KEY`) |
| `RATE_LIMIT` | `60` | Max evaluations per session per minute. Set to `0` to disable. |
| `DATA_DIR` | `~/.intaris` | Base directory for database and event store files |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Authentication

At least one of `INTARIS_API_KEY`, `INTARIS_API_KEYS`, `INTARIS_JWT_PUBLIC_KEY`, or `INTARIS_JWKS_URL` should be set in production. Without any of them, the server accepts unauthenticated requests.

With `INTARIS_API_KEY` (single shared key), clients must also send `X-User-Id` to identify themselves. With `INTARIS_API_KEYS`, the user identity is resolved from the key mapping. With Cognis JWTs, Intaris resolves `user_id` from the JWT `sub` claim and optional `agent_id` from the JWT `agent_id` claim (or `X-Agent-Id` when the claim is absent).

Clients authenticate via `Authorization: Bearer <key-or-jwt>` header or `X-API-Key: <key>` header.

For Cognis integration, configure one JWT verifier source:

```bash
INTARIS_JWT_PUBLIC_KEY=/path/to/cognis-public.pem
# or
INTARIS_JWKS_URL=https://cognis.example.com/.well-known/jwks.json
```

JWT validation requires:

- `iss="cognis"`
- `aud` includes `"intaris"`
- `sub` claim present
- matching `agent_id` claim and `X-Agent-Id` header when both are provided

## LLM (Safety Evaluation)

| Variable | Default | Description |
|---|---|---|
| `LLM_API_KEY` | (required) | API key for the LLM provider. Falls back to `OPENAI_API_KEY`. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API base URL. Falls back to `OPENAI_API_BASE`. |
| `LLM_MODEL` | `gpt-5.4-nano` | Model for safety evaluation. Should be fast and cheap. |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort hint (provider-specific). |
| `LLM_TIMEOUT_MS` | `4000` | Timeout for LLM calls in milliseconds. Must be under the 5-second circuit breaker. Minimum: 500ms. |

### Model Selection

The evaluation model should be fast and inexpensive -- it's called on every non-read-only tool call. `gpt-5.4-nano` or similar small models work well. The model must support structured output (JSON mode).

## LLM (L2 Behavioral Analysis)

Separate LLM configuration for L2 analysis tasks (session summaries). Typically a more capable model with longer timeout than the evaluation model.

| Variable | Default | Description |
|---|---|---|
| `ANALYSIS_LLM_API_KEY` | (falls back to `LLM_API_KEY`) | API key for analysis LLM |
| `ANALYSIS_LLM_BASE_URL` | (falls back to `LLM_BASE_URL`) | Base URL for analysis LLM |
| `ANALYSIS_LLM_MODEL` | `gpt-5.4-mini` | Model for L2 analysis tasks |
| `ANALYSIS_LLM_REASONING_EFFORT` | `low` | Reasoning effort for analysis |
| `ANALYSIS_LLM_TIMEOUT_MS` | `30000` | Timeout for analysis LLM calls (30s) |

## LLM (L3 Cross-Session Analysis)

Separate LLM configuration for L3 cross-session behavioral analysis. L3 detects subtle patterns across sessions (progressive escalation, coordinated access, intent masking) and typically uses a more capable model than L2. Falls back to the L2 analysis config, then the evaluate LLM config.

| Variable | Default | Description |
|---|---|---|
| `ANALYSIS_L3_LLM_API_KEY` | (falls back to `ANALYSIS_LLM_API_KEY`, then `LLM_API_KEY`) | API key for L3 analysis |
| `ANALYSIS_L3_LLM_BASE_URL` | (falls back to `ANALYSIS_LLM_BASE_URL`, then `LLM_BASE_URL`) | Base URL for L3 analysis |
| `ANALYSIS_L3_LLM_MODEL` | (falls back to `ANALYSIS_LLM_MODEL`, then `gpt-5.4`) | Model for L3 analysis |
| `ANALYSIS_L3_LLM_REASONING_EFFORT` | (falls back to `ANALYSIS_LLM_REASONING_EFFORT`) | Reasoning effort for L3 analysis |
| `ANALYSIS_L3_LLM_TIMEOUT_MS` | (falls back to `ANALYSIS_LLM_TIMEOUT_MS`, default `30000`) | Timeout for L3 analysis LLM calls |

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
| `ANALYSIS_LOOKBACK_DAYS` | `7` | Days of history to include in cross-session analysis |
| `ANALYSIS_WORKER_COUNT` | `4` | Number of parallel task queue workers |
| `ANALYSIS_WINDOW_CHARS` | `150000` | Max chars per L2 analysis window prompt. The partitioner creates more windows when data exceeds this budget. Tune for smaller/larger model context windows. |
| `ANALYSIS_L3_WINDOW_CHARS` | `200000` | Max chars per L3 cross-session analysis prompt. Progressive summarization compresses older sessions when the prompt exceeds this budget. |

## Barriers

| Variable | Default | Description |
|---|---|---|
| `INTENTION_BARRIER_TIMEOUT_MS` | `1000` | Max time (ms) the evaluate endpoint waits for a pending intention update |
| `INTENTION_BARRIER_POLL_TIMEOUT_MS` | `10000` | Max time (ms) the evaluate endpoint waits for `/reasoning` to arrive when `/evaluate` races ahead of the user-message trigger |
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

## Judge Auto-Resolution

| Variable | Default | Description |
|---|---|---|
| `JUDGE_MODE` | `disabled` | Judge mode: `disabled` (no judge), `auto` (approve/deny, deny if uncertain), `advisory` (approve/deny/defer to human) |
| `JUDGE_NOTIFY_MODE` | `deny_only` | When to notify in judge mode: `deny_only` (judge denials only), `always` (judge approvals/denials and final unresolved escalations after defer/failure), `never` (fully silent) |

### LLM (Judge)

The judge uses a more capable model (default gpt-5.4) with longer timeout than the evaluate model. Falls back to the evaluate LLM for base URL and API key.

When `JUDGE_MODE` is enabled and the evaluator escalates, `POST /evaluate` waits for the judge. This means `JUDGE_LLM_TIMEOUT_MS` contributes directly to request latency for those escalated calls. If the judge fails or times out, the request degrades to an unresolved `escalate` for human review.

| Variable | Default | Description |
|---|---|---|
| `JUDGE_LLM_MODEL` | `gpt-5.4` | LLM model for judge reviews |
| `JUDGE_LLM_BASE_URL` | (falls back to `LLM_BASE_URL`) | LLM base URL for judge |
| `JUDGE_LLM_API_KEY` | (falls back to `LLM_API_KEY`) | LLM API key for judge |
| `JUDGE_LLM_REASONING_EFFORT` | `low` | Reasoning effort for judge LLM |
| `JUDGE_LLM_TIMEOUT_MS` | `15000` | Timeout for judge LLM calls in milliseconds |

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
