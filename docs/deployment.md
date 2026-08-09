# Deployment

Production deployment guide for Intaris.

## Requirements

- Python 3.11, 3.12, or 3.13
- An OpenAI-compatible LLM API key

## Installation

### pip

```bash
pip install -e .
```

### Docker

```bash
docker compose up -d
```

The included `docker-compose.yaml` provides a minimal setup:

```yaml
services:
  intaris:
    build: .
    ports:
      - "8060:8060"
      - "127.0.0.1:9090:9090"
    environment:
      - LLM_API_KEY=${LLM_API_KEY:-${OPENAI_API_KEY}}
    volumes:
      - intaris-data:/data
    restart: unless-stopped

volumes:
  intaris-data:
```

### Docker Image Details

The Dockerfile uses a two-stage build for fast dependency caching:

- Base image: `python:3.13-slim`
- Uses `uv` for fast dependency resolution
- Data directory: `/data` (mount a volume here)
- Exposed ports: `8060` for the main API and `9090` for metrics
- Health check: `GET /health` every 30s
- Entry point: `intaris`

### Custom Docker Compose

For production, extend the compose file with all required configuration:

```yaml
services:
  intaris:
    build: .
    ports:
      - "8060:8060"
      - "127.0.0.1:9090:9090"
    environment:
      # LLM
      - LLM_API_KEY=sk-your-key
      - LLM_MODEL=gpt-5.4-nano
      - LLM_TIMEOUT_MS=4000

      # Authentication
      - INTARIS_API_KEYS={"key-alice": "alice@example.com", "key-bob": "bob@example.com"}

      # Analysis LLM (optional, more capable model)
      - ANALYSIS_LLM_MODEL=gpt-5.4-mini
      - ANALYSIS_LLM_TIMEOUT_MS=30000

      # Webhook (optional, for Cognis integration)
      - WEBHOOK_URL=https://cognis.example.com/webhook
      - WEBHOOK_SECRET=your-webhook-secret

      # MCP proxy (optional)
      - INTARIS_ENCRYPTION_KEY=your-fernet-key
      - MCP_CONFIG_FILE=/config/mcp-servers.json

      # Notifications
      - NOTIFICATION_ACTION_TTL_MINUTES=60
    volumes:
      - intaris-data:/data
      - ./mcp-servers.json:/config/mcp-servers.json:ro
    restart: unless-stopped

volumes:
  intaris-data:
```

## Authentication

Intaris supports both standalone API keys and Cognis-issued ES256 JWTs.

### Single Shared Key

Simplest setup -- one key for all clients:

```bash
export INTARIS_API_KEY=your-secret-key
```

Clients must send `X-User-Id` header to identify themselves. Suitable for single-user setups.

### Multi-Key with User Mapping (Recommended)

Map API keys to user identities:

```bash
export INTARIS_API_KEYS='{"key-for-alice": "alice@example.com", "key-for-bob": "bob@example.com", "admin-key": "*"}'
```

- Each key maps to a `user_id` -- no `X-User-Id` header needed
- A value of `"*"` means auth-only (no user binding) -- useful for admin keys that can impersonate users via `X-User-Id`
- All sessions and audit records are scoped to the resolved `user_id`

### Cognis JWT Service Auth

When Cognis is the caller, configure Intaris to trust Cognis-issued JWTs:

```bash
export INTARIS_JWT_PUBLIC_KEY=/path/to/cognis-public.pem
# or
export INTARIS_JWKS_URL=https://cognis.example.com/.well-known/jwks.json
```

Intaris expects:

- `Authorization: Bearer <jwt>`
- `iss="cognis"`
- `aud` includes `"intaris"`
- `sub` = user identity
- optional `agent_id` claim (falls back to `X-Agent-Id` when absent)

API keys remain supported for direct standalone clients.

### No Authentication

Without `INTARIS_API_KEY` or `INTARIS_API_KEYS`, the server accepts unauthenticated requests. Only suitable for local development.

## Storage

### Database

SQLite database stored at `DB_PATH` (default `~/.intaris/intaris.db`, or `/data/intaris.db` in Docker). Uses WAL mode for concurrent read/write.

For Docker deployments, mount a volume at `/data` to persist the database across container restarts.

### Event Store

Session recordings are stored as chunked ndjson files. Two backends are available:

**Filesystem (default):**

```bash
export EVENT_STORE_BACKEND=filesystem
export EVENT_STORE_PATH=/data/events  # default: ~/.intaris/events
```

**S3/MinIO:**

```bash
export EVENT_STORE_BACKEND=s3
export EVENT_STORE_S3_ENDPOINT=https://s3.amazonaws.com
export EVENT_STORE_S3_ACCESS_KEY=your-access-key
export EVENT_STORE_S3_SECRET_KEY=your-secret-key
export EVENT_STORE_S3_BUCKET=intaris-events
export EVENT_STORE_S3_REGION=us-east-1
export EVENT_CACHE_BACKEND=filesystem
export EVENT_CACHE_PATH=/data/event-cache
export EVENT_CACHE_MAX_BYTES=10737418240
```

Both backends use the same chunked layout: `{user_id}/{session_id}/seq_{start:06d}_{end:06d}.ndjson`.
Only the S3 backend uses `EVENT_CACHE_*`. It keeps immutable chunks locally
after the first read. Each replica can maintain an independent cache.

## Reverse Proxy

Intaris serves HTTP on port 8060. For HTTPS, place it behind a reverse proxy:

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name intaris.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8060;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket support
    location /api/v1/stream {
        proxy_pass http://localhost:8060;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }

    # MCP proxy (SSE support)
    location /mcp {
        proxy_pass http://localhost:8060;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300;
    }
}
```

Key considerations:
- WebSocket endpoint (`/api/v1/stream`) needs `Upgrade` and `Connection` headers
- MCP endpoint (`/mcp`) needs SSE support (disable buffering)
- Set appropriate read timeouts for long-lived connections

## Webhook Integration

For external approval systems (e.g., Cognis), configure webhook callbacks:

```bash
export WEBHOOK_URL=https://cognis.example.com/api/v1/webhook
export WEBHOOK_SECRET=your-hmac-secret
export INTARIS_BASE_URL=https://intaris.example.com
```

Webhooks are HMAC-SHA256 signed. The signature is sent in the `X-Intaris-Signature` header as `sha256=<hex_digest>`. The payload includes `call_id`, `session_id`, `user_id`, `agent_id`, `tool`, `args_redacted`, `risk`, `reasoning`, and `intaris_url`.

## Notification Channels

Per-user notification channels are configured via the REST API (not environment variables):

```bash
# Pushover
curl -X PUT http://localhost:8060/api/v1/notifications/channels/my-phone \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"provider": "pushover", "config": {"user_key": "...", "api_token": "..."}, "events": ["escalation", "session_suspended"]}'

# Slack
curl -X PUT http://localhost:8060/api/v1/notifications/channels/my-slack \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"provider": "slack", "config": {"webhook_url": "https://hooks.slack.com/..."}, "events": ["escalation"]}'
```

Notification messages include one-click approve/deny action links with time-limited Fernet tokens (configurable via `NOTIFICATION_ACTION_TTL_MINUTES`, default 60 minutes).

## MCP Server Configuration

For the MCP proxy feature, define upstream servers via a config file:

```bash
export MCP_CONFIG_FILE=/path/to/mcp-servers.json
export INTARIS_ENCRYPTION_KEY=your-fernet-key  # required for servers with secrets
```

See the [MCP Proxy Guide](mcp-proxy.md) for config file format and details.

## Performance Tuning

| Setting | Default | Recommendation |
|---|---|---|
| `LLM_TIMEOUT_MS` | `4000` | Keep under 5s (circuit breaker). Lower for faster models. |
| `RATE_LIMIT` | `60` | Increase for high-throughput agents. Set to `0` to disable. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `30` | Lower for faster cleanup of abandoned sessions. |
| `EVENT_STORE_FLUSH_SIZE` | `100` | Increase for high-volume recording. |
| `EVENT_STORE_FLUSH_BYTES` | `4194304` | Target an upper bound for serialized chunk size. |
| `EVENT_STORE_FLUSH_INTERVAL` | `30` | Decrease for lower-latency playback. |
| `EVENT_STORE_S3_CHUNK_CACHE_TTL` | `300` | Increase for single-replica deployments; reduce when other writers must be discovered without shared invalidation. |
| `EVENT_CACHE_BACKEND` | `filesystem` | Set to `disabled` to bypass immutable S3 chunk caching. |
| `EVENT_CACHE_PATH` | `~/.intaris/event-cache` | Use fast local storage with sufficient free space. |
| `EVENT_CACHE_MAX_BYTES` | `10737418240` | Bound local cache disk use. |
| `EVENT_CACHE_TTL_SECONDS` | `604800` | Remove chunks that remain unused for this interval. |

## Monitoring

The `/health` endpoint returns service status and can be used for health checks:

```bash
curl http://localhost:8060/health
```

The response also includes process-local performance metrics:

- HTTP request counts and latency histograms by route template, method, and status group.
- Event-loop scheduling delay.
- Database query, transaction, and connection-pool wait latency.
- Event-store flush latency, batch sizes, buffered events, and reconciliation progress.
- S3 request counts, transferred bytes, operation latency, metadata-cache behavior,
  and immutable chunk-cache hits, misses, bytes, evictions, and load latency.

Counters reset when the process restarts. In a multi-replica deployment, collect and
aggregate the response from each replica.

Prometheus-compatible metrics are available without authentication:

```bash
curl http://localhost:9090/metrics
```

The metrics listener is unauthenticated and separate from the main API. Do not
publish `METRICS_PORT` through the public Service or ingress. Prometheus can
scrape the pod port directly.

The `/api/v1/stats` endpoint provides aggregated metrics for monitoring dashboards.
