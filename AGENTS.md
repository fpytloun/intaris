# AGENTS.md — Coding Agent Instructions for intaris

## Project Overview

**intaris** is a guardrails service for AI coding agents, providing safety evaluation, audit logging, and approval workflows for tool calls. It evaluates whether agent tool calls are safe and aligned with the session's declared intention.

- **Language**: Python 3.11+
- **Framework**: FastAPI/Starlette for HTTP, OpenAI-compatible API for LLM
- **Core dependencies**: fastapi, uvicorn, starlette, openai
- **License**: Apache 2.0
- **Repository**: https://github.com/fpytloun/intaris
- **Part of**: Cognara platform (Cognis controller, Intaris guardrails, Mnemory memory)

## Architecture

```
intaris/
├── integrations/
│   ├── opencode/
│   │   ├── intaris.ts     # TypeScript plugin (tool.execute.before interception)
│   │   ├── opencode.json  # MCP proxy config example
│   │   └── README.md      # Setup and usage guide
│   └── claude-code/
│       ├── hooks.json     # Hook configuration (SessionStart + PreToolUse)
│       ├── scripts/
│       │   ├── session.sh # SessionStart handler (creates Intaris session)
│       │   └── evaluate.sh # PreToolUse handler (calls /api/v1/evaluate)
│       └── README.md      # Setup and usage guide
├── server.py              # HTTP server entry point, health endpoint, auth middleware, lifespan, MCP mount
├── config.py              # Configuration from environment variables (dataclasses)
├── crypto.py              # Fernet encryption/decryption for secrets at rest
├── db.py                  # SQLite connection management, table creation, indexes, migrations
├── session.py             # Session CRUD + counter updates (paginated list)
├── audit.py               # Audit log storage + querying (with args_hash for escalation retry)
├── classifier.py          # Tool call classification (read/write/critical/escalate)
├── redactor.py            # Secret redaction for audit args
├── llm.py                 # OpenAI-compatible LLM client with structured output
├── prompts.py             # Safety evaluation prompt templates + JSON schema
├── evaluator.py           # Evaluation pipeline orchestrator (ESCALATE branch, retry, args_hash)
├── decision.py            # Decision matrix (priority-ordered, escalate fast path)
├── ratelimit.py           # In-memory sliding window rate limiter
├── webhook.py             # Async webhook client with HMAC-SHA256 signing
├── mcp/
│   ├── __init__.py        # Package marker
│   ├── store.py           # MCPServerStore CRUD + tool preferences (encrypted secrets)
│   ├── config.py          # File-based config loader with orphan reconciliation
│   ├── client.py          # MCPConnectionManager (upstream connections, idle sweep)
│   └── proxy.py           # MCP Server with list_tools/call_tool handlers
├── api/
│   ├── __init__.py        # FastAPI sub-app factory
│   ├── deps.py            # SessionContext dependency (identity from ContextVars)
│   ├── schemas.py         # Pydantic request/response models
│   ├── evaluate.py        # POST /api/v1/evaluate (rate limiting, webhook, EventBus)
│   ├── intention.py       # POST /api/v1/intention, GET /api/v1/session/{id}, GET /sessions, PATCH /session/{id}/status
│   ├── audit.py           # GET /api/v1/audit, POST /api/v1/decision (EventBus publish)
│   ├── info.py            # GET /whoami, /stats, /config (management UI support, MCP stats)
│   ├── mcp.py             # MCP server CRUD + tool preference endpoints
│   └── stream.py          # EventBus + WebSocket streaming (first-message auth)
└── ui/
    ├── __init__.py        # Package marker
    ├── tailwind.config.js # Tailwind CSS brand theme config
    ├── src/
    │   └── input.css      # Tailwind source CSS with component classes
    └── static/
        ├── index.html     # Single-page app (Alpine.js + Tailwind, 6 tabs)
        ├── css/
        │   └── app.css    # Pre-built Tailwind output (committed)
        ├── js/
        │   ├── api.js     # IntarisAPI client singleton (with MCP methods)
        │   ├── app.js     # Alpine.js stores (auth, nav, notify)
        │   ├── dashboard.js # Dashboard tab component
        │   ├── sessions.js  # Sessions tab component
        │   ├── audit.js     # Audit tab component
        │   ├── approvals.js # Approvals tab component (WebSocket + polling fallback)
        │   ├── servers.js   # Servers tab component (MCP server management)
        │   └── settings.js  # Settings tab component
        └── vendor/
            └── alpine.min.js # Vendored Alpine.js (no CDN)
```

### Layer responsibilities

| Layer | File | Responsibility |
|---|---|---|
| **Transport** | `server.py` | HTTP routing, auth middleware (ContextVars), health endpoint, lifespan init |
| **Identity** | `api/deps.py` | SessionContext dependency (user_id, agent_id from ContextVars) |
| **REST API** | `api/` | FastAPI endpoints with OpenAPI spec |
| **Streaming** | `api/stream.py` | EventBus (pub/sub) + WebSocket endpoint with first-message auth |
| **Info** | `api/info.py` | Identity (/whoami), stats (/stats), config (/config) for management UI |
| **Orchestration** | `evaluator.py` | Full evaluation pipeline (classify → LLM → decide → audit) |
| **Classification** | `classifier.py` | Read-only allowlist, critical patterns, session policy |
| **Decision** | `decision.py` | Priority-ordered decision matrix |
| **LLM** | `llm.py` | OpenAI-compatible client with structured output |
| **Prompts** | `prompts.py` | Safety evaluation prompt templates |
| **Redaction** | `redactor.py` | Secret redaction before audit storage |
| **Rate Limiting** | `ratelimit.py` | In-memory sliding window rate limiter per (user_id, session_id) |
| **Webhook** | `webhook.py` | Async webhook client with HMAC-SHA256 signing for escalation callbacks |
| **Session** | `session.py` | Session CRUD, counter management, paginated listing |
| **Audit** | `audit.py` | Audit log CRUD and querying |
| **Database** | `db.py` | SQLite connection management, schema |
| **Configuration** | `config.py` | Environment variable parsing |

### Key design decisions

1. **Default-deny classifier**: Explicit read-only allowlist. Everything not allowlisted goes through LLM evaluation. Unknown tools and third-party MCP tools are always classified as WRITE.

2. **Priority-ordered decision matrix**: Critical risk → deny (always). Aligned + low/medium → approve. Aligned + high → escalate. Not aligned → escalate. LLM deny → deny.

3. **Standalone escalation**: Without Cognis, escalations are denied with message directing user to Intaris UI. With Cognis, escalations go through webhook callback to approval queue.

4. **Secret redaction**: All tool args are redacted before audit storage. Pattern-based (API keys, passwords, connection strings, JWTs, private keys) + key-name-based (password, token, secret, etc.).

5. **5-second circuit breaker constraint**: The Executor Adapter has a 5-second timeout. LLM_TIMEOUT_MS defaults to 4000ms to ensure Intaris responds within the window.

6. **Session policy extensibility**: Sessions can define custom allow/deny rules using glob patterns (fnmatch, NOT regex) to avoid ReDoS.

7. **Follows mnemory conventions**: Same build system (hatchling), config pattern (dataclasses + env vars), LLM client (OpenAI wrapper with structured output), error handling, and code style.

8. **Multi-tenancy**: `user_id` is the tenant separator — scopes all sessions and audit records. `agent_id` is metadata only (not a visibility boundary). See [Multi-tenancy](#multi-tenancy) below.

## Multi-tenancy

Intaris uses `user_id` as the tenant separator. Every session and audit record is scoped to a `user_id`, and all database queries include a `WHERE user_id = ?` clause.

### Identity resolution

Identity is resolved by the `APIKeyMiddleware` in `server.py` and propagated via ContextVars:

1. **API key mapping** (`INTARIS_API_KEYS`): JSON dict `{"api-key": "username", "key2": "*"}`. A value of `"*"` means the key authenticates but does not bind to a specific user.
2. **Single shared key** (`INTARIS_API_KEY`): Authenticates but does not bind to a user.
3. **Header fallback**: When the API key does not bind a user, `X-User-Id` header is accepted.
4. **Agent ID**: Always from `X-Agent-Id` header (optional metadata, not a visibility boundary).

### ContextVars → SessionContext

The middleware sets three ContextVars (`_session_user_id`, `_session_agent_id`, `_session_user_bound`), always reset in a `finally` block. API endpoints use `Depends(get_session_context)` from `api/deps.py` to get a `SessionContext` dataclass with `user_id`, `agent_id`, and `user_bound` fields.

### Key differences from mnemory

- `agent_id` is **metadata only** — the human operator sees all their sessions/audit across all agents. No dual-scope pattern.
- Simpler identity model: no agent-scoped visibility boundaries.

### Environment variables

| Variable | Description |
|---|---|
| `INTARIS_API_KEY` | Single shared API key (auth only, no user binding) |
| `INTARIS_API_KEYS` | JSON dict mapping API keys to user_ids (`{"key": "user", "key2": "*"}`) |
| `RATE_LIMIT` | Max evaluations per session per minute (default 60, 0 = no limit) |
| `WEBHOOK_URL` | Cognis webhook URL for escalation callbacks (optional) |
| `WEBHOOK_SECRET` | HMAC-SHA256 secret for signing webhook payloads (required if WEBHOOK_URL is set) |
| `WEBHOOK_TIMEOUT_MS` | Webhook HTTP timeout in milliseconds (default 3000) |
| `INTARIS_BASE_URL` | Base URL for constructing `intaris_url` in webhook payloads (optional) |
| `INTARIS_ENCRYPTION_KEY` | Fernet key for encrypting MCP server secrets at rest (required if secrets present) |
| `MCP_CONFIG_FILE` | Path to JSON file defining MCP servers (optional, reconciled on startup) |
| `MCP_ALLOW_STDIO` | Allow stdio transport for MCP servers (default `false`) |
| `MCP_UPSTREAM_TIMEOUT_MS` | Timeout for upstream MCP server calls in milliseconds (default `30000`) |

## Build / Run / Test

### Local development

```bash
pip install -e ".[dev]"
export LLM_API_KEY=sk-your-key
intaris
```

### Tests

```bash
# Unit tests (fast, no API key needed)
pytest tests/ -v

# E2e tests (require LLM_API_KEY, real LLM calls)
pytest -m e2e -v

# Both
pytest -m '' -v
```

### Linting

```bash
ruff check intaris/ tests/
ruff format intaris/ tests/
```

## Code Conventions

### Style

- Python 3.11+ features (type unions with `|`, `from __future__ import annotations`)
- Type hints on all function signatures
- Docstrings on all public classes and methods
- `logging` module for all output (never `print()`)
- f-strings for string formatting

### Error handling

- API endpoints catch ValueError (→ 4xx) and Exception (→ 500)
- Internal errors logged with `logger.exception()` for stack traces
- Evaluator catches LLM failures and treats them as escalation (safe default)

### Configuration

- All config via environment variables (no config files)
- Dataclass-based config objects in `config.py`
- `load_config()` validates required fields at startup
- Defaults optimized for local development

### Database

- SQLite with WAL mode for concurrent read/write
- Thread-local connections via `threading.local()`
- Foreign keys enabled
- Sessions use compound PK `(user_id, session_id)` for tenant isolation
- Audit log uses compound FK `(user_id, session_id)` referencing sessions
- Indexes on `audit_log(user_id, session_id, timestamp)`, `audit_log(decision)`, `audit_log(record_type)`

### Audit record types

The `audit_log` table supports multiple record types via the `record_type` column:

| Type | Description | Key fields |
|---|---|---|
| `tool_call` | Standard tool call evaluation (default) | `tool`, `args_redacted`, `classification` |
| `reasoning` | Agent reasoning checkpoint (future) | `content` |
| `checkpoint` | Periodic agent state checkpoint (future) | `content` |

For `tool_call` records, `tool`, `args_redacted`, and `classification` are populated. For `reasoning` and `checkpoint` records, `content` holds the evaluated text and tool-specific fields are null. All record types share `decision`, `risk`, `reasoning`, `evaluation_path`, and `latency_ms`.

## Rate Limiting

Per-session sliding window rate limiter (`ratelimit.py`). Tracks call timestamps per `(user_id, session_id)` pair using a deque. Thread-safe via `threading.Lock`. Configured via `RATE_LIMIT` env var (default 60 calls/minute, 0 = disabled). Periodic sweep removes abandoned session entries every 5 minutes.

The rate limit check runs **before** classification/LLM in the evaluate endpoint. Returns HTTP 429 when exceeded.

## Webhook Callbacks

Async webhook client (`webhook.py`) for notifying Cognis about escalations. Uses `httpx.AsyncClient` with HMAC-SHA256 payload signing. Fire-and-forget via `asyncio.create_task()` in the evaluate endpoint — does not block the response.

- Payload includes: `call_id`, `session_id`, `user_id`, `agent_id`, `tool`, `args_redacted`, `risk`, `reasoning`, `intaris_url`
- Signature: `X-Intaris-Signature` header with `sha256=<hex_digest>` of the JSON body
- Single retry on failure (HTTP error or timeout)
- `Config.validate()` raises if `WEBHOOK_URL` is set but `WEBHOOK_SECRET` is empty

## WebSocket Streaming

Real-time event streaming via WebSocket at `/api/v1/stream` (`api/stream.py`).

### EventBus

In-memory pub/sub with `(user_id, session_id)` keyed subscribers. Bounded `Queue(1000)` per subscriber — drops oldest events on overflow. Events are published from the evaluate and audit endpoints.

### WebSocket Protocol

Uses **first-message auth** (no secrets in URLs):

1. Client connects to `ws://host/api/v1/stream`
2. Client sends: `{"type": "auth", "token": "Bearer ...", "user_id": "...", "session_id": "..."}`
3. Server validates token and subscribes to EventBus
4. Server streams events as JSON messages
5. Server sends `{"type": "ping"}` every 30s as keepalive (client may ignore; pong is not enforced)
6. Per-user connection limit: 10 concurrent WebSocket connections
7. Auth failure closes with code 4001

### Session Status Enforcement

The evaluator checks session status **before** any classification or LLM work. Suspended or terminated sessions are immediately denied with an appropriate message. This is enforced in `evaluator.py` at the start of the `evaluate()` method.

## E2E Tests

End-to-end tests (`tests/test_e2e.py`) use real LLM calls to verify the full evaluation pipeline. They require `OPENAI_API_KEY` or `LLM_API_KEY` and are excluded from the default `pytest` run.

### Running

```bash
pytest -m e2e -v                    # e2e only
pytest -m e2e -v --timeout=60       # with generous timeout
pytest -m '' -v                     # all tests (unit + e2e)
```

### Test categories (30 tests)

| Category | Tests | Description |
|---|---|---|
| **Fast paths** | 2 | Read-only auto-approve, critical auto-deny |
| **Aligned approval** | 5 | Clearly aligned tool calls → approve via LLM |
| **Misaligned** | 5 | Misaligned tool calls → deny or escalate via LLM |
| **Dangerous operations** | 4 | Malicious operations → strict deny |
| **Same tool, different context** | 2 | Same command, different intention → different outcome |
| **Session lifecycle** | 3 | Status enforcement, counter accuracy |
| **Audit trail** | 3 | Record creation, secret redaction, filtering |
| **Escalation workflow** | 2 | Escalation → resolution → audit verification |
| **Session policy** | 2 | Policy allow/deny overrides classification |
| **High risk** | 2 | Aligned but risky operations |

### Assertion strategy

- **Strict** (`== "deny"` or `== "approve"`): Used for clear-cut cases where any reasonable LLM should agree (e.g., malicious operations → deny, clearly aligned → approve).
- **Pragmatic** (`!= "approve"`): Used for borderline cases where the LLM might reasonably choose deny or escalate (e.g., misaligned but not dangerous).

### Default LLM config

- Model: `gpt-5-nano` (fast, cheap, sufficient for safety evaluation)
- Reasoning effort: `low`
- Temperature: `0.1`
- Timeout: `4000ms`

## Built-in Management UI

Single-page web UI served at `/ui` for monitoring and managing Intaris. Built with Alpine.js + Tailwind CSS, following the same pattern as the mnemory project's UI.

### Architecture

- **No build step at runtime**: Alpine.js is vendored (`static/vendor/alpine.min.js`), Tailwind CSS is pre-built and committed (`static/css/app.css`).
- **Tab-based navigation**: 6 tabs — Dashboard, Sessions, Audit, Approvals, Servers, Settings.
- **Auth**: API key stored in `localStorage`, sent via `X-API-Key` header. User impersonation via `X-User-Id` header when `can_switch_user` is true.
- **Real-time updates**: Approvals tab uses WebSocket (`/api/v1/stream`) for real-time updates with 10s polling fallback.

### Tabs

| Tab | Description | API endpoints used |
|---|---|---|
| **Dashboard** | Stat cards, decision distribution, recent activity | `GET /stats`, `GET /audit` |
| **Sessions** | Filterable session list with expandable detail | `GET /sessions`, `GET /session/{id}`, `GET /audit`, `PATCH /session/{id}/status` |
| **Audit** | Filterable audit log table with expandable detail | `GET /audit` |
| **Approvals** | Pending escalations with approve/deny actions | `GET /audit?decision=escalate&resolved=false`, `POST /decision` |
| **Servers** | MCP server management — add/edit/delete upstream servers, tool preferences | `GET/PUT/DELETE /mcp/servers/{name}`, `GET/PUT/DELETE /mcp/servers/{name}/tools/{tool}/preference` |
| **Settings** | Read-only server configuration display | `GET /config` |

### API endpoints for UI

| Endpoint | Description |
|---|---|
| `GET /api/v1/whoami` | Identity verification + `can_switch_user` flag |
| `GET /api/v1/stats` | Aggregated dashboard metrics (sessions, evaluations, decisions, users) |
| `GET /api/v1/config` | Non-sensitive server config (LLM base URL masked) |

### Rebuilding CSS

After modifying `intaris/ui/src/input.css` or any HTML/JS files in `intaris/ui/static/`, rebuild the Tailwind output:

```bash
npx tailwindcss -i intaris/ui/src/input.css -o intaris/ui/static/css/app.css --minify
```

The pre-built `app.css` is committed to the repository so no build step is needed at runtime.

### Static file serving

- `server.py` mounts `StaticFiles` at `/ui` with `html=True` for `index.html` fallback.
- Graceful degradation: only mounts if `ui/static/` directory exists and is non-empty.
- Auth middleware skips `/ui` paths (static files don't need auth; API calls use `X-API-Key` from `localStorage`).
- Redirect from `/ui` → `/ui/` for consistent URL handling.

## MCP Proxy

Intaris can act as an MCP proxy, sitting between LLM clients (Claude Code, OpenCode, Cursor, etc.) and upstream MCP servers. Every tool call is evaluated through the safety pipeline before being forwarded upstream.

### How it works

1. Client connects to Intaris at `/mcp` using the MCP Streamable HTTP transport.
2. Client calls `tools/list` → Intaris aggregates tools from all configured upstream servers, namespaced as `server_name:tool_name`.
3. Client calls `tools/call` → Intaris evaluates the call through the full safety pipeline (classify → LLM → decide → audit), then forwards approved calls to the upstream server.
4. Escalated calls return an `isError: true` result with a message directing the user to the Intaris UI for approval.

### Modules

| Module | Responsibility |
|---|---|
| `mcp/store.py` | CRUD for MCP server configs and tool preferences. Secrets encrypted at rest via `crypto.py`. Name validation: `^[a-zA-Z0-9][a-zA-Z0-9_-]*$`, max 64 chars. |
| `mcp/config.py` | Loads server definitions from a JSON file (`MCP_CONFIG_FILE`). Reconciles orphaned `source="file"` entries on restart. |
| `mcp/client.py` | `MCPConnectionManager` — lazy upstream connections with 30-min idle timeout, max 10 connections per user, background sweep task. Supports stdio, HTTP, and SSE transports. |
| `mcp/proxy.py` | `MCPProxy` — MCP server that handles `tools/list` and `tools/call`. Auto-creates sessions, aggregates tools with 5-min cache, routes calls through the evaluator. |
| `crypto.py` | Fernet encrypt/decrypt for secrets at rest. `ValueError` if `INTARIS_ENCRYPTION_KEY` missing when secrets are present. |

### Tool namespacing

Tools are namespaced as `server_name:tool_name` (colon separator). When a client calls a namespaced tool, the proxy strips the prefix, routes to the correct upstream server, and forwards the original tool name.

### Classification with tool preferences

Tool preferences (`mcp_tool_preferences` table) allow per-tool overrides of the default classification behavior. Priority chain (deny-first):

1. Session policy deny → **DENY**
2. Tool preference deny → **DENY**
3. Tool preference escalate → **ESCALATE**
4. Session policy allow → classification from allowlist/patterns
5. Tool preference auto-approve → **READ** (skip LLM)
6. Critical patterns → **CRITICAL**
7. Read-only allowlist → **READ**
8. Default → **WRITE** (goes through LLM evaluation)

### Escalation retry

When a tool call is escalated and later approved, subsequent identical calls (same tool + same args) reuse the approval for 10 minutes. Identity is based on SHA-256 of `json.dumps(args, sort_keys=True, separators=(',', ':'))`, stored in `audit_log.args_hash`.

### Session auto-creation

The MCP proxy auto-creates sessions for new connections. Intention is resolved in priority order:
1. `X-Intaris-Intention` request header
2. `server_instructions` from the MCP initialize request
3. Default: `"MCP proxy session — evaluate all tool calls for safety"`

### Transports

| Transport | Config key | Description |
|---|---|---|
| `stdio` | `command`, `args`, `env` | Subprocess-based. Requires `MCP_ALLOW_STDIO=true`. |
| `streamable-http` | `url`, `headers` | HTTP-based (MCP SDK `streamablehttp_client`). |
| `sse` | `url`, `headers` | Server-Sent Events (MCP SDK `sse_client`). |

### File-based configuration

Servers can be defined in a JSON file pointed to by `MCP_CONFIG_FILE`. Format:

```json
{
  "users": {
    "user@example.com": {
      "mcpServers": {
        "my-server": {
          "type": "streamable-http",
          "url": "https://example.com/mcp",
          "headers": {"Authorization": "Bearer token"},
          "agent_pattern": "*"
        }
      }
    }
  }
}
```

File-defined servers are stored with `source="file"` in the database. On startup, the config loader reconciles: new servers are inserted, existing ones are updated, and orphaned file-sourced entries (removed from the file) are deleted.

### REST API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/mcp/servers` | GET | List all configured MCP servers |
| `/api/v1/mcp/servers/{name}` | GET | Get a single server config |
| `/api/v1/mcp/servers/{name}` | PUT | Create or update a server |
| `/api/v1/mcp/servers/{name}` | DELETE | Delete a server |
| `/api/v1/mcp/servers/{name}/refresh` | POST | Force-refresh tools cache from upstream |
| `/api/v1/mcp/servers/{name}/tools/{tool}/preference` | GET | Get tool preference |
| `/api/v1/mcp/servers/{name}/tools/{tool}/preference` | PUT | Set tool preference |
| `/api/v1/mcp/servers/{name}/tools/{tool}/preference` | DELETE | Remove tool preference |

### Database tables

- **`mcp_servers`**: `name` (PK), `user_id`, `transport`, `config_json` (encrypted if secrets), `enabled`, `source` ("api"/"file"), `tools_cache`, `tools_cache_at`, `created_at`, `updated_at`.
- **`mcp_tool_preferences`**: `server_name` + `tool_name` (compound PK), `user_id`, `preference` (CHECK: auto-approve/escalate/deny), `created_at`.
- **`audit_log.args_hash`**: SHA-256 column for escalation retry lookup. Indexed via `idx_audit_escalation_retry(user_id, tool, args_hash, decision)`.

## Important Notes

- **Evaluation pipeline**: classify → critical check → LLM → decision matrix → audit. Fast path skips LLM for read-only and critical classifications.
- **LLM timeout**: Configured via `LLM_TIMEOUT_MS` (default 4000ms). Must be under the 5-second circuit breaker in the Executor Adapter.
- **Session policy**: Uses fnmatch glob patterns (NOT regex) for custom allow/deny rules to prevent ReDoS attacks.
- **Redaction immutability**: `redact()` always returns a deep copy. Never mutates input args.
- **Sub-app state propagation**: The Starlette parent app initializes `rate_limiter`, `webhook`, `event_bus`, and `mcp_proxy` in its lifespan, then propagates them to the FastAPI sub-app's `state`. This is necessary because `request.app` in FastAPI endpoints refers to the sub-app, not the parent.

## Integrations

Client integrations live in `integrations/` and provide two approaches for each tool:

1. **REST API plugin** (recommended): Intercepts tool calls in the client and evaluates them via `POST /api/v1/evaluate`. Gives fine-grained control over error messages, fail-open/fail-closed behavior, and session lifecycle.
2. **MCP proxy**: Configures the client to point at Intaris's `/mcp` endpoint. Zero code — just configuration. Full MCP proxy features (tool preferences, escalation retry, namespacing).

**Do not use both approaches simultaneously** — tool calls would be evaluated twice.

### OpenCode

- **Plugin**: `integrations/opencode/intaris.ts` — TypeScript plugin using `tool.execute.before` hook. Creates Intaris sessions on `session.created`, evaluates every tool call before execution.
- **MCP config**: `integrations/opencode/opencode.json` — Remote MCP server pointing at `/mcp`.
- **Env vars**: `INTARIS_URL`, `INTARIS_API_KEY`, `INTARIS_AGENT_ID` (default: `opencode`), `INTARIS_USER_ID`, `INTARIS_FAIL_OPEN` (default: `false`), `INTARIS_INTENTION`.
- **Install**: Copy `intaris.ts` to `~/.config/opencode/plugins/` (global) or `.opencode/plugins/` (project).

### Claude Code

- **Hooks**: `integrations/claude-code/hooks.json` — `SessionStart` creates session, `PreToolUse` evaluates tool calls.
- **Scripts**: `integrations/claude-code/scripts/session.sh` and `evaluate.sh` — Bash scripts using `curl` and `jq`.
- **Env vars**: Same as OpenCode, plus `INTARIS_DEBUG` (default: `false`) for stderr logging.
- **Install**: Copy scripts to `~/.claude/scripts/`, merge `hooks.json` into `~/.claude/settings.json`.

### Tool name conventions

Different clients use different tool naming conventions:

| Client | Built-in tools | MCP tools |
|---|---|---|
| **OpenCode** | `read`, `edit`, `write`, `bash` | MCP tool name directly (e.g., `add_memory`) |
| **Claude Code** | `Read`, `Edit`, `Write`, `Bash` (capitalized) | `mcp__server__tool` (double underscore, e.g., `mcp__mnemory__add_memory`) |
| **Intaris MCP proxy** | N/A | `server_name:tool_name` (colon, e.g., `mnemory:add_memory`) |

Session policies (fnmatch patterns) must use the naming convention of the integration approach being used.
