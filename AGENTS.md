# AGENTS.md — Coding Agent Instructions for intaris

## Project Overview

**intaris** is a guardrails service for AI coding agents, providing safety evaluation, audit logging, and approval workflows for tool calls. It evaluates whether agent tool calls are safe and aligned with the session's declared intention.

- **Language**: Python 3.11+
- **Framework**: FastAPI/Starlette for HTTP, OpenAI-compatible API for LLM
- **Core dependencies**: fastapi, uvicorn, starlette, openai
- **License**: BSL 1.1 (converts to Apache 2.0 on 2030-03-15)
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
│       ├── hooks.json     # Hook configuration (SessionStart + PreToolUse + PostToolUse + Stop)
│       ├── scripts/
│       │   ├── session.sh # SessionStart handler (creates Intaris session)
│       │   ├── evaluate.sh # PreToolUse handler (calls /api/v1/evaluate + checkpoints + recording)
│       │   ├── record.sh  # PostToolUse handler (records tool results when recording enabled)
│       │   └── stop.sh    # Stop handler (session completion + agent summary + transcript upload)
│       └── README.md      # Setup and usage guide
├── server.py              # HTTP server entry point, health endpoint, auth middleware, lifespan, MCP mount
├── config.py              # Configuration from environment variables (dataclasses)
├── crypto.py              # Fernet encryption/decryption for secrets at rest
├── db.py                  # SQLite connection management, table creation, indexes, migrations
├── session.py             # Session CRUD + counter updates (paginated list, idle sweep)
├── audit.py               # Audit log storage + querying (with args_hash for escalation retry)
├── classifier.py          # Tool call classification (read/write/critical/escalate)
├── redactor.py            # Secret redaction for audit args
├── llm.py                 # OpenAI-compatible LLM client with structured output
├── prompts.py             # Safety evaluation prompt templates + JSON schema
├── prompts_analysis.py    # L2/L3 analysis prompt templates + JSON schemas
├── intention.py           # IntentionBarrier (user-driven intention updates) + generate_intention()
├── evaluator.py           # Evaluation pipeline orchestrator (ESCALATE branch, retry, behavioral context)
├── alignment.py           # AlignmentBarrier (parent/child intention enforcement via LLM)
├── analyzer.py            # Stub: L2 summary generation + L3 behavioral analysis (Phase 2)
├── background.py          # TaskQueue (SQLite), BackgroundWorker (idle sweep, scheduler), Metrics
├── decision.py            # Decision matrix (priority-ordered, escalate fast path)
├── ratelimit.py           # In-memory sliding window rate limiter
├── webhook.py             # Async webhook client with HMAC-SHA256 signing
├── events/
│   ├── __init__.py        # Package marker
│   ├── backend.py         # EventBackend Protocol, FilesystemEventBackend, S3EventBackend
│   └── store.py           # EventStore (high-level wrapper with EventBuffer, EventBus integration)
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
│   ├── analysis.py        # Behavioral analysis endpoints (L1/L2/L3 data collection + retrieval)
│   ├── events.py          # Session recording endpoints (POST/GET events, flush)
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
        │   ├── player.js    # Session recording player component
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
| **Intention** | `intention.py` | IntentionBarrier (user-driven intention updates) + generate_intention() |
| **Orchestration** | `evaluator.py` | Full evaluation pipeline (classify → LLM → decide → audit), behavioral context injection |
| **Alignment** | `alignment.py` | AlignmentBarrier (parent/child intention enforcement via LLM) |
| **Classification** | `classifier.py` | Read-only allowlist, critical patterns, session policy |
| **Decision** | `decision.py` | Priority-ordered decision matrix |
| **LLM** | `llm.py` | OpenAI-compatible client with structured output |
| **Prompts** | `prompts.py` | Safety evaluation prompt templates |
| **Analysis Prompts** | `prompts_analysis.py` | L2/L3 analysis prompt templates + JSON schemas (window + compaction) |
| **Analyzer** | `analyzer.py` | L2 hierarchical summary generation (window + compaction) + L3 agent-scoped cross-session analysis |
| **Background** | `background.py` | TaskQueue (SQLite-backed), BackgroundWorker (idle sweep, scheduler), Metrics |
| **Redaction** | `redactor.py` | Secret redaction before audit storage |
| **Rate Limiting** | `ratelimit.py` | In-memory sliding window rate limiter per (user_id, session_id) |
| **Webhook** | `webhook.py` | Async webhook client with HMAC-SHA256 signing for escalation callbacks |
| **Event Store** | `events/` | Session recording: chunked ndjson storage, write buffering, EventBus integration |
| **Session** | `session.py` | Session CRUD, counter management, paginated listing, idle sweep |
| **Audit** | `audit.py` | Audit log CRUD and querying (reasoning, checkpoint, summary record types) |
| **Database** | `db.py` | SQLite connection management, schema (incl. analysis tables) |
| **Configuration** | `config.py` | Environment variable parsing (incl. AnalysisConfig, EventStoreConfig) |

### Key design decisions

1. **Default-deny classifier**: Explicit read-only allowlist. Everything not allowlisted goes through LLM evaluation. Unknown tools and third-party MCP tools are always classified as WRITE.

2. **Priority-ordered decision matrix**: Critical risk → deny (always). Aligned + low/medium → approve. Aligned + high → escalate. Not aligned → escalate. LLM deny → deny.

3. **Standalone escalation**: Without Cognis, escalations are denied with message directing user to Intaris UI. With Cognis, escalations go through webhook callback to approval queue.

4. **Secret redaction**: All tool args are redacted before audit storage. Pattern-based (API keys, passwords, connection strings, JWTs, private keys) + key-name-based (password, token, secret, etc.).

5. **5-second circuit breaker constraint**: The Executor Adapter has a 5-second timeout. LLM_TIMEOUT_MS defaults to 4000ms to ensure Intaris responds within the window.

6. **Session policy extensibility**: Sessions can define custom allow/deny rules using glob patterns (fnmatch, NOT regex) to avoid ReDoS. Includes `allow_paths`/`deny_paths` for filesystem path policy.

10. **Filesystem path protection**: When `working_directory` is set on a session, the classifier checks file paths in tool arguments against the project boundary. Read-only tools targeting paths outside the project are reclassified as WRITE (forcing LLM evaluation). The evaluator maintains an approved path prefix cache that learns from LLM approvals — once a sibling project is approved, subsequent reads are fast-pathed. See [Filesystem Path Protection](#filesystem-path-protection) below.

7. **Follows mnemory conventions**: Same build system (hatchling), config pattern (dataclasses + env vars), LLM client (OpenAI wrapper with structured output), error handling, and code style.

8. **Multi-tenancy**: `user_id` is the tenant separator — scopes all sessions and audit records. `agent_id` is metadata only (not a visibility boundary). See [Multi-tenancy](#multi-tenancy) below.

9. **User-driven intention model**: Session intention is immutable except by user action. Agent tool calls never redefine intention. The `IntentionBarrier` pattern ensures the evaluator sees the freshest user-stated intention by coordinating between `/reasoning` (trigger) and `/evaluate` (wait). See [Intention Model](#intention-model) below.

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
| `INTENTION_BARRIER_TIMEOUT_MS` | Max time (ms) the evaluate endpoint waits for a pending intention update (default `1000`) |
| `INTENTION_BARRIER_POLL_TIMEOUT_MS` | Max time (ms) the evaluate endpoint waits for `/reasoning` to arrive when `intention_pending=true` (default `2000`) |
| `ALIGNMENT_BARRIER_TIMEOUT_MS` | Max time (ms) the evaluate endpoint waits for a pending alignment check (default `15000`) |
| `ANALYSIS_ENABLED` | Enable behavioral analysis pipeline (default `true`) |
| `SESSION_IDLE_TIMEOUT_MINUTES` | Minutes of inactivity before session transitions to idle (default `30`) |
| `SUMMARY_VOLUME_THRESHOLD` | Evaluate calls per session before triggering a summary (default `50`) |
| `ANALYSIS_INTERVAL_MINUTES` | Minutes between periodic cross-session analysis runs (default `60`) |
| `ANALYSIS_LOOKBACK_DAYS` | Days of history to include in cross-session analysis (default `7`) |
| `ANALYSIS_LLM_MODEL` | LLM model for analysis tasks (default `gpt-5-mini`) |
| `ANALYSIS_LLM_BASE_URL` | LLM base URL for analysis (falls back to `LLM_BASE_URL`) |
| `ANALYSIS_LLM_API_KEY` | LLM API key for analysis (falls back to `LLM_API_KEY`) |
| `ANALYSIS_LLM_REASONING_EFFORT` | Reasoning effort for analysis LLM (default `low`) |
| `ANALYSIS_LLM_TIMEOUT_MS` | Timeout for analysis LLM calls in milliseconds (default `30000`) |
| `ANALYSIS_WORKER_COUNT` | Number of parallel task queue workers (default `4`) |
| `ANALYSIS_WINDOW_CHARS` | Max chars per L2 analysis window prompt (default `150000`). The partitioner creates more windows when data exceeds this budget — no data is silently dropped. Tune for smaller/larger model context windows. |
| `ANALYSIS_L3_WINDOW_CHARS` | Max chars per L3 cross-session analysis prompt (default `200000`). Progressive summarization compresses older sessions when the prompt exceeds this budget. |
| `ANALYSIS_L3_LLM_MODEL` | LLM model for L3 cross-session analysis (falls back to `ANALYSIS_LLM_MODEL`, then `gpt-5.4`) |
| `ANALYSIS_L3_LLM_BASE_URL` | LLM base URL for L3 analysis (falls back to `ANALYSIS_LLM_BASE_URL`, then `LLM_BASE_URL`) |
| `ANALYSIS_L3_LLM_API_KEY` | LLM API key for L3 analysis (falls back to `ANALYSIS_LLM_API_KEY`, then `LLM_API_KEY`) |
| `ANALYSIS_L3_LLM_REASONING_EFFORT` | Reasoning effort for L3 analysis LLM (falls back to `ANALYSIS_LLM_REASONING_EFFORT`) |
| `ANALYSIS_L3_LLM_TIMEOUT_MS` | Timeout for L3 analysis LLM calls (falls back to `ANALYSIS_LLM_TIMEOUT_MS`, default `30000`) |
| `NOTIFICATION_ACTION_TTL_MINUTES` | TTL for notification action tokens in minutes (default `60`) |

## Build / Run / Test

### Local development

```bash
uv pip install -e ".[dev]"
export LLM_API_KEY=sk-your-key
uv run intaris
```

Or use the Makefile:

```bash
make dev          # install with dev deps
make test         # unit tests
make lint         # ruff check
make format       # ruff format
make css          # rebuild Tailwind CSS
make css-watch    # Tailwind watch mode
```

### Tests

```bash
# Unit tests (fast, no API key needed)
uv run pytest tests/ -v

# E2e tests (require LLM_API_KEY, real LLM calls)
uv run pytest -m e2e -v

# Both
uv run pytest -m '' -v
```

### Linting

```bash
uv run ruff check intaris/ tests/
uv run ruff format intaris/ tests/
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
- Evaluator propagates LLM failures as exceptions (→ 500), letting clients retry

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
| `reasoning` | Agent reasoning text | `content` |
| `checkpoint` | Periodic agent state checkpoint | `content` |
| `summary` | Session summary record | `content` |

For `tool_call` records, `tool`, `args_redacted`, and `classification` are populated. For `reasoning`, `checkpoint`, and `summary` records, `content` holds the text and tool-specific fields are null. All record types share `decision`, `risk`, `reasoning`, `evaluation_path`, and `latency_ms`. The `profile_version` column records the behavioral profile version at time of evaluation.

### Evaluation paths

The `evaluation_path` column indicates how the decision was reached:

| Path | Description |
|---|---|
| `fast` | Read-only auto-approve, escalation retry, or session status deny |
| `critical` | Critical pattern auto-deny |
| `llm` | LLM safety evaluation → decision matrix |
| `alignment` | Alignment misalignment escalation (child session intention conflicts with parent) |

## Intention Model

Session intention is **user-driven only** — only user messages, explicit declarations, and client-side title changes can update it. Agent tool calls never redefine intention.

### IntentionBarrier pattern

The `IntentionBarrier` (`intention.py`) coordinates between the `/reasoning` and `/evaluate` endpoints to ensure the evaluator always sees the freshest user-stated intention:

1. **Trigger**: When `POST /reasoning` receives a user message (content starts with `"User message:"`), it calls `barrier.trigger()` which starts an async LLM task to regenerate the intention.
2. **Wait**: When `POST /evaluate` runs, it calls `await barrier.wait()` before invoking the evaluator. If an intention update is pending, it blocks up to `INTENTION_BARRIER_TIMEOUT_MS` (default 1s).
3. **Arrival wait**: When the client sends `intention_pending=true` in the evaluate request but `/reasoning` hasn't arrived yet (race condition), the barrier waits up to `INTENTION_BARRIER_POLL_TIMEOUT_MS` (default 2s) for `trigger()` to be called. Uses `asyncio.Event` for zero-latency wakeup — no polling.
4. **Cancel-and-restart**: If a new user message arrives while an update is running, the old task is cancelled and a fresh one starts. Only the latest message's update runs to completion.

Budget (Claude Code hooks): 1s barrier + 4s LLM eval = 5s max (within the circuit breaker constraint).
Budget (OpenCode plugin): 2s arrival wait + 1s barrier + 4s LLM eval = 7s max (within 30s plugin timeout).

### Intention sources

The `intention_source` column on sessions tracks how the intention was set:

| Source | Description |
|---|---|
| `initial` | Set at session creation (default) |
| `user` | Updated from a user message via the IntentionBarrier |
| `bootstrap` | One-time refinement from tool patterns (see below) |

### One-time bootstrap

Sessions that never receive user messages (e.g., Claude Code, MCP proxy) keep their generic initial intention. At evaluate call 10, if `intention_source` is still `"initial"`, a single refinement fires via the background task queue. This is capped at exactly one update to prevent agent drift from rewriting the intention.

### generate_intention()

Shared function in `intention.py` used by both the IntentionBarrier (immediate path) and the background worker (bootstrap path). Uses the analysis LLM to summarize what the session is about based on user messages (primary signal) and recent tool calls (secondary). Accepts an injected `LLMClient` rather than creating one per call.

### AlignmentBarrier pattern

The `AlignmentBarrier` (`alignment.py`) enforces parent/child session intention compatibility using the same barrier pattern as the IntentionBarrier. When a child session is created (or its intention is updated), an async LLM alignment check runs. The first `POST /evaluate` call waits for the check to complete before proceeding.

**Flow:**

1. **Trigger at creation**: `POST /intention` with `parent_session_id` → validates parent exists → creates session (status=active) → triggers `alignment_barrier.trigger()` async.
2. **Trigger on intention update**: When `IntentionBarrier` completes an intention update for a child session, or when `PATCH /session/{id}` updates a child session's intention, a re-check is triggered via `alignment_barrier.trigger()`. The `alignment_overridden` flag is cleared before re-triggering so the new intention is re-evaluated.
3. **Wait at evaluate**: `POST /evaluate` calls `await alignment_barrier.wait()` after the intention barrier wait. If a check is pending, it blocks up to `ALIGNMENT_BARRIER_TIMEOUT_MS` (default 15s).
4. **Escalate on misalignment**: If the LLM determines the child intention contradicts the parent, the barrier stores the misalignment in memory. The evaluator returns `decision=escalate` with `evaluation_path=alignment` for subsequent tool calls. The client handles this like any other escalation (poll for user approval via `POST /decision`).
5. **User acknowledgment**: When the user approves the alignment escalation via `POST /decision`, the `alignment_overridden` flag is set on the session (persisted to DB). Subsequent tool calls proceed through normal LLM evaluation with `parent_intention` injected as defense-in-depth.
6. **Fail-open**: If the LLM call fails or times out, the session stays active. Per-call LLM evaluation still catches misaligned WRITE calls.

**Timing budget (worst case, first evaluate of new child session):**
- Intention barrier wait: up to 1s
- Alignment barrier wait: up to 15s
- LLM evaluation: up to 4s
- Total: 20s max (within OpenCode plugin's 30s evaluate timeout)

This worst case only applies to the very first evaluate call. Subsequent calls have no barrier waits.

**Parent lifecycle cascade**: The evaluator also checks parent session status on every evaluate call for child sessions. If the parent is terminated or suspended, the child is auto-suspended with `status_reason = "Parent session is {status}"`.

**Client-side handling** (OpenCode plugin):
- `decision === "escalate"` with `path === "alignment"` → enters the standard escalation polling loop (poll `GET /audit/{call_id}` with exponential backoff). When user approves → tool call proceeds. When user denies → tool call blocked.
- `session_status === "suspended"` → only for parent lifecycle cascade (parent terminated/suspended). Enter polling loop for session status.
- `session_status === "terminated"` → throw error immediately (hard kill).
- Reuses `INTARIS_ESCALATION_TIMEOUT` for max wait time.

**Design principles:**
- No tool calls execute before alignment is verified (barrier blocks first evaluate)
- Misalignment → escalation (not suspension) so the user can approve via standard UI
- Cancel-and-restart on re-trigger (e.g., intention update while check is in flight)
- User acknowledgment persisted via `alignment_overridden` column (survives server restart)
- Intention changes clear the override flag and re-trigger alignment check
- `alignment_overridden` restored from DB on startup; active non-overridden child sessions re-checked

| Env Var | Default | Description |
|---|---|---|
| `ALIGNMENT_BARRIER_TIMEOUT_MS` | `15000` | Max time evaluate waits for alignment check |

## Behavioral Analysis

Three-layer behavioral guardrails system that evolves Intaris from a per-call firewall into a behavioral analysis platform. Supports hierarchical sessions — parent sessions incorporate child session data into their summaries and compacted session-level assessments.

### Architecture

```
L1: Data Collection (per-call)
  POST /reasoning    — agent reasoning text → audit_log (record_type="reasoning")
  POST /checkpoint   — agent state snapshot → audit_log (record_type="checkpoint")
  Evaluator          — auto-updates session last_activity_at, injects behavioral context

L2: Session Analysis (per-session, hierarchical)
  POST /session/{id}/agent-summary  — agent-reported summary → agent_summaries table
  POST /session/{id}/summary/trigger — manual trigger → enqueue summary task
  GET  /session/{id}/summary        — retrieve both Intaris + agent summaries
  BackgroundWorker                  — idle sweep, volume triggers, periodic scheduling
  generate_summary()                — tail window + compaction + child orchestration
  _generate_compaction()            — synthesize windows into session-level summary

L3: Cross-Session Analysis (per-user, root sessions only)
  POST /analysis/trigger — manual trigger → enqueue analysis task
  GET  /analysis         — list behavioral analyses
  GET  /profile          — behavioral risk profile (user-bound API key only)
  run_analysis()         — agent-scoped, parent_session_id IS NULL filter
```

### Key design principles

1. **Agent data isolation**: Agent-reported text (reasoning, checkpoints, summaries) is NEVER included in Intaris analysis prompts. Stored for post-hoc comparison only. Sanitized on ingestion to strip prompt injection patterns.

2. **Separate LLM config**: Analysis uses a dedicated LLM configuration (`ANALYSIS_LLM_*` env vars) — typically a more capable model with longer timeout than the evaluate model. Falls back to the evaluate LLM key/base_url if not explicitly set.

3. **SQLite task queue**: Background tasks (summaries, analyses) use a SQLite-backed task queue (`analysis_tasks` table) with atomic claim/complete/fail operations, retry with exponential backoff, and duplicate detection.

4. **Resilient background loops**: All background worker loops are wrapped in `_resilient_loop()` with exponential backoff (5s → 60s) and automatic restart on failure.

5. **Profile access control**: `GET /profile` requires `user_bound=True` (API key maps to a specific user). This prevents agents from querying their own risk profile.

6. **Behavioral context injection**: The evaluator looks up the pre-computed behavioral profile (~1ms DB read) and injects `behavioral_alert` into the evaluation context for high/critical risk profiles.

7. **Hierarchical summaries**: Parent sessions collect child session data (compacted > window > raw metadata) and include it in both window and compaction prompts. Child sessions are summarized independently first, then their data flows up to the parent.

8. **Summary compaction**: Multiple window summaries are synthesized into a single session-level compacted summary. Compaction uses supersede semantics — old compacted summaries are deleted and replaced. Compaction runs automatically when a session has > 1 window summaries.

9. **Task-queue orchestration**: Parent summary tasks check for unsummarized children, enqueue child tasks at higher priority, and re-enqueue themselves with a 30s delay (max 5 re-enqueues). This avoids recursive LLM calls blocking the single-threaded task queue.

### Hierarchical summary flow

```
Session close / idle / volume trigger
  → generate_summary(parent_session)
    → Check children: any need summaries?
      → YES: return needs_children signal
        → BackgroundWorker enqueues child tasks (priority 3)
        → BackgroundWorker re-enqueues parent (priority 2, 30s delay)
      → NO: proceed
    → Generate tail window summary (if enough data)
    → Collect child data (compacted > window > metadata fallback)
    → Generate compaction (if > 1 windows exist)
      → Delete old compacted summary (supersede)
      → Insert new compacted summary
```

### Summary types

| Type | Description | When created |
|---|---|---|
| `window` | Covers a time range since the last window summary | During active sessions (volume trigger, idle, manual) |
| `compacted` | Synthesizes all windows into one session-level summary | On session close, idle, or when > 1 windows exist |

The `_get_window_start()` function filters to `summary_type='window'` only — compacted summaries span the full session and must not prevent new window generation after session resume.

### Risk indicator categories

- **intent_drift**: Agent gradually shifting away from declared intention
- **restriction_circumvention**: Attempts to work around denied operations
- **scope_creep**: Accessing resources beyond the expected project scope
- **insecure_reasoning**: Agent reasoning that suggests unsafe decision-making
- **unusual_tool_pattern**: Unexpected tool usage sequences or frequencies
- **injection_attempt**: Signs of prompt injection in tool args or reasoning
- **escalation_pattern**: Increasing frequency of denied or escalated calls
- **delegation_misalignment**: A sub-session's actions or intention diverge from the parent session's declared intention

### L3 cross-session analysis

L3 analysis operates only on **root sessions** (`parent_session_id IS NULL`) in both the agent-scoped and all-agents query paths. Child session data is already embedded in parent compacted summaries, so including children separately would double-count.

When fetching session summaries for L3, compacted summaries are preferred over window summaries. If a session has a compacted summary, only that is included. Otherwise, all window summaries are used.

L3 uses a **separate LLM configuration** (`ANALYSIS_L3_LLM_*` env vars) — typically a more capable model (default `gpt-5.4`) than the L2 model.  Falls back to the L2 analysis config, then the evaluate LLM config.

L3 uses **progressive summarization** to stay within context budget — see [L3 progressive summarization](#l3-progressive-summarization) below.

### Database tables (analysis)

| Table | Key columns | Description |
|---|---|---|
| `session_summaries` | `summary_type` (window/compacted), `trigger` (incl. compaction) | Intaris-generated L2 session summaries |
| `agent_summaries` | | Agent-reported session summaries (isolated from analysis) |
| `behavioral_analyses` | `agent_id` | L3 cross-session analysis results |
| `behavioral_profiles` | `(user_id, agent_id)` | Pre-computed per-user risk profiles |
| `analysis_tasks` | | SQLite-backed task queue for background processing |

Key indexes: `idx_sessions_parent ON sessions(user_id, parent_session_id)` for efficient child session lookups.

### Hierarchy constants

| Constant | Value | Description |
|---|---|---|
| `_MAX_CHILD_SESSIONS` | 20 | Max children included in parent summary (breadth limit) |
| `_MAX_COMPACTION_WINDOWS` | 50 | Max window summaries in compaction prompt (token budget) |
| `_MAX_SUMMARY_CHARS` | 3000 | Max chars per child/prior summary narrative |
| `_MAX_PARENT_RECHECK` | 5 | Max re-enqueue attempts for parent waiting on children |
| `_PARENT_RECHECK_DELAY_S` | 30 | Seconds between parent re-enqueue checks |

### Analyzer constants

Budget-aware windowing and per-entry safety valves.  No content is ever silently dropped — when data exceeds the window budget, more windows (and LLM calls) are created.  Per-entry safety valves are rarely-hit safeguards that append explicit truncation metadata and content security flags when triggered.

| Constant | Value | Description |
|---|---|---|
| `_MAX_WINDOW_CHARS` | 150,000 | Max chars per L2 window prompt (configurable via `ANALYSIS_WINDOW_CHARS`). Designed for 200k-token models. |
| `_CONTEXT_OVERHEAD` | 8,000 | Subtracted from window budget for system prompt + output tokens |
| `_L3_MAX_WINDOW_CHARS` | 200,000 | Max chars per L3 analysis prompt (configurable via `ANALYSIS_L3_WINDOW_CHARS`) |
| `_L3_RECENT_DAYS` | 3 | Sessions within this range get full detail in L3; older sessions get compressed format |
| `_ENTRY_CONTENT_LIMIT` | 5,000 | Per-entry safety valve for reasoning entries |
| `_WRITE_ARGS_CONTENT_LIMIT` | 2,000 | Per-entry safety valve for WRITE/CRITICAL tool args content |
| `_READ_ARGS_CONTENT_LIMIT` | 200 | Per-entry limit for READ tool args (compressed anyway) |
| `_ARGS_VALUE_LIMIT` | 1,000 | Per-entry limit for generic arg values |
| `_EVAL_REASONING_LIMIT` | 500 | Intaris evaluation reasoning brief |
| `_USER_NOTE_LIMIT` | 500 | Human user's escalation notes |

### Content security scanning

Defense-in-depth: `_scan_content_flags()` scans the **full, untruncated** tool args for security-sensitive patterns and produces compact flag strings (e.g., `["ssh_access", "code_exec"]`).  Flags are appended to every WRITE/CRITICAL tool call line in analysis prompts, ensuring dangerous patterns are visible even when content is summarized.

Detected patterns: `system_files`, `ssh_access`, `remote_exec`, `code_exec`, `dangerous_rm`, `permission_mod`, `network_bind`, `encoding`, `cron_modification`, `firewall_mod`, `credential_access`, `env_exfiltration`.

Static regex patterns are evadable by sophisticated attackers.  The primary defense is budget-aware partitioning ensuring all data reaches an LLM analysis window.  Content security flags are a supplementary signal.

### Safety valve system

`_apply_safety_valve()` replaces silent truncation with explicit metadata.  When a per-entry limit is exceeded, the truncated text includes a suffix showing what was omitted and any content security flags from the full content:

```
[2000 of 47000 chars shown flags:code_exec,ssh_access]
```

Safety valve activations are counted via `drain_safety_valve_hits()` and propagated to `Metrics.safety_valve_hits_total` by the background worker after each task.

### L3 progressive summarization

L3 cross-session analysis uses progressive summarization to stay within the context budget.  Sessions are tiered by recency:

- **Recent** (within `_L3_RECENT_DAYS`): Full summary narrative + all risk indicators with detail + tools list
- **Older**: Intention + alignment + stats + indicator categories only (no narrative, no detail)

If the prompt exceeds the L3 budget after progressive summarization, the recent threshold is reduced to 1 day before falling back.  The L3 system prompt instructs the LLM to weight recent sessions more heavily.

### Cross-window aggregates in compaction

Compaction prompts include a "Cross-Window Aggregates" section that surfaces cumulative patterns invisible at the per-window level: total calls, denial/escalation rates, alignment trajectory across windows, and risk indicator frequency.  This partially mitigates distributed attacks where malicious activity stays below detection threshold in each individual window.

### Classification-aware args summarization

Tool call arguments are summarized differently based on classification:

- **File paths**: Never truncated (the full path is the security signal)
- **WRITE/CRITICAL content**: Up to `_WRITE_ARGS_CONTENT_LIMIT` chars + content security flags
- **READ content**: Brief summaries (compressed into batch counts anyway)
- **Generic values**: Up to `_ARGS_VALUE_LIMIT` chars

### Observability metrics

| Metric | Description |
|---|---|
| `summary_child_triggers_total` | Total child summary tasks enqueued by parent orchestration |
| `summary_max_children_per_task` | High-water mark of children per parent task |
| `summary_parent_recheck_count` | Total parent re-enqueue cycles |
| `compaction_total` | Total compacted summaries generated |
| `safety_valve_hits_total` | Total safety valve activations (content truncated with metadata) |

### Session lifecycle extensions

- **`idle` status**: Sessions transition to `idle` after `SESSION_IDLE_TIMEOUT_MINUTES` of inactivity (background sweep). Idle sessions are auto-resumed on the next evaluate call.
- **`completed` denial**: Completed, suspended, and terminated sessions deny all evaluations.
- **`last_activity_at`**: Updated on every evaluate, reasoning, and checkpoint call.
- **`parent_session_id`**: Optional field for session continuation chains.
- **`summary_count`**: Tracks number of summaries generated for a session (both window and compacted).
- **`intention_source`**: Tracks how the intention was set (`initial`, `user`, `bootstrap`). See [Intention Model](#intention-model).

### Phase status

- **Phase 1 (Foundation)**: Complete — infrastructure, data collection endpoints, background worker, evaluator integration.
- **Phase 2 (LLM Analysis)**: Complete — `generate_summary()` implements windowed, iterative session summaries analyzing three data streams (user messages, tool calls, agent reasoning with anti-injection sandboxing). Hierarchical session support with child data collection and task-queue orchestration.
- **Phase 2.5 (Summary Compaction)**: Complete — `_generate_compaction()` synthesizes multiple window summaries into a single session-level assessment with supersede semantics. Parent sessions incorporate child session data. New `delegation_misalignment` risk indicator for sub-session divergence.
- **Phase 3 (Profile Updates)**: Complete — `run_analysis()` implements agent-scoped cross-session analysis. Profiles keyed by `(user_id, agent_id)`. Evaluator injects `behavioral_alert` for high/critical profiles. L3 filters to root sessions only and prefers compacted summaries.
- **Phase 4 (UI Integration)**: Complete — Analysis tab (profile + analysis history), session summaries in Sessions detail (compacted prominent, windows expandable), behavioral risk indicator in Dashboard.
- **Phase 5 (Tuning)**: Not started — threshold tuning, alert rules, notification integration from analysis findings.

## Filesystem Path Protection

When `working_directory` is set in session details (sent by integrations at session creation), the classifier and evaluator enforce filesystem path boundaries.

### Classification priority chain (with path steps)

| Step | Check | Result |
|---|---|---|
| 1 | Session policy `deny_tools` / `deny_commands` | CRITICAL |
| **1.5** | **Session policy `deny_paths` (fnmatch on resolved paths)** | **CRITICAL** |
| 2 | Tool preference deny | CRITICAL |
| 3 | Tool preference escalate | ESCALATE |
| 4 | Session policy `allow_tools` / `allow_commands` | READ |
| 5 | Tool preference auto-approve | READ |
| 6 | Critical patterns (bash) | CRITICAL |
| 7 | Read-only allowlist | READ |
| **7.5** | **Path outside project (not in `allow_paths`)** | **WRITE** |
| 8 | Default | WRITE |

### Path extraction

File paths are extracted from tool arguments using known keys: `filePath`, `file_path`, `path`, `directory`, `dir`, `folder`, `filename`. For read-only bash commands, absolute paths are also extracted from the command string using a simple regex.

Relative paths are resolved against `working_directory` using `os.path.normpath()` to prevent `../../../etc/shadow` traversal bypasses.

### Session policy path fields

```json
{
    "allow_paths": ["/home/user/other-project/*", "/tmp/*"],
    "deny_paths": ["/etc/*", "/root/*"]
}
```

- `deny_paths`: fnmatch patterns. Matched paths → CRITICAL (auto-deny). Checked at step 1.5 (highest priority deny). Always checked, even when the approved paths cache has entries.
- `allow_paths`: fnmatch patterns. Matched paths are exempt from the out-of-project WRITE override at step 7.5. Does NOT override `deny_paths`.

### Approved path prefix cache (learning from approvals)

The evaluator maintains an in-memory cache of approved path prefixes per session. When a read-only tool call that was reclassified to WRITE due to path policy is approved — either by the LLM directly or by a user resolving an escalation — the evaluator caches the approved directory prefix. Subsequent reads under that prefix are fast-pathed as READ without LLM evaluation.

**Two learning paths:**
- **LLM approval**: When the LLM approves a path-reclassified read, the prefix is cached immediately in the evaluate pipeline.
- **User-approved escalation**: When a user approves an escalated read via `POST /decision`, the endpoint calls `evaluator.learn_from_approved_escalation()` which extracts file paths from the audit record, looks up the session's `working_directory`, and caches the prefix.

**Prefix computation** uses a depth-aware heuristic:
- **Sibling projects** (deep common ancestor with `working_directory`): prefix is one level deeper than the common ancestor. E.g., working in `/src/mnemory`, reading `/src/intaris/file.py` → prefix `/src/intaris`.
- **Distant paths** (shallow common ancestor): prefix is the exact parent directory of the target file. E.g., reading `/var/log/app.log` → prefix `/var/log` (NOT `/var/`).

**Prefix merging**: When a new prefix shares a deep common ancestor (≥ 4 path components) with an existing cached prefix, the two are merged into the common ancestor. This naturally broadens the cache as the agent explores related paths. E.g., approving `.../@opencode-ai/sdk/dist` then `.../@opencode-ai/plugin/dist` → merged to `.../@opencode-ai`.

**Cache properties:**
- In-memory, session-scoped (keyed by `(user_id, session_id)`)
- No TTL — persists for session lifetime
- Cleared on session completion/termination/suspension
- Max 50 prefixes per session (FIFO eviction)
- Thread-safe via `threading.Lock()`
- `deny_paths` always checked BEFORE the cache (deny wins over approval)
- Periodic sweep removes entries for dead sessions
- New prefixes already covered by a broader cached prefix are skipped

### LLM context injection

When a WRITE-classified tool call goes to LLM evaluation and `working_directory` is set, the evaluator injects `project_path` into the evaluation context. The LLM system prompt instructs it to consider whether file operations are within the expected project scope.

**User decision history**: The recent tool call history shown to the LLM includes user decisions on resolved escalations. Instead of just `[escalate]`, the LLM sees `[escalate→user:approve]` or `[escalate→user:deny]`. The system prompt instructs the LLM to treat user-approved escalations as strong precedent — if a user approved a read to a specific directory, similar reads to the same or sibling directories should be approved rather than re-escalated.

### Backward compatibility

All path logic is gated on `working_directory is not None`. Sessions without `working_directory` (e.g., MCP proxy sessions that don't set it) behave exactly as before — no path checking, no overrides.

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

## Session Recording (Event Store)

Full-fidelity session recording system that captures the complete timeline of AI agent sessions as append-only ndjson event logs. Enables live tailing via WebSocket, session playback in the UI, and deeper behavioral analysis.

### Architecture

```
Client (OpenCode/Claude Code)
  → POST /session/{id}/events (batch append)
  → EventStore.append() (seq assignment, buffering, EventBus publish)
  → EventBuffer (in-memory, per-session)
  → FilesystemEventBackend / S3EventBackend (chunked ndjson)
```

### Event format

Each ndjson line:
```json
{"seq": 1, "ts": "2026-03-12T10:00:00.123Z", "type": "message", "source": "opencode", "data": {...}}
```

### Canonical event types

`message`, `tool_call`, `tool_result`, `evaluation`, `part`, `lifecycle`, `checkpoint`, `reasoning`, `transcript`

### Storage

Chunked ndjson files, one chunk per flush. Filename encodes seq range: `{user_id}/{session_id}/seq_{start:06d}_{end:06d}.ndjson`. Both filesystem and S3 backends use identical layout.

### API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/session/{id}/events` | POST | Append events (single or batch) |
| `/api/v1/session/{id}/events` | GET | Read events with pagination and filtering |
| `/api/v1/session/{id}/events/flush` | POST | Force flush buffered events |

### Auto-append

Existing endpoints auto-append to the event store as a side effect:
- `/evaluate` → `evaluation` events
- `/reasoning` → `reasoning` events
- `/checkpoint` → `checkpoint` events
- Session lifecycle → `lifecycle` events (created, status changed)

### Client-side recording

- **OpenCode plugin**: `INTARIS_SESSION_RECORDING=true` enables client-side buffering (50 events or 10s) with batch sends. Captures `tool_call`, `tool_result`, `message`, and `part` events.
- **Claude Code hooks**: `INTARIS_SESSION_RECORDING=true` enables per-hook recording. `PostToolUse` records tool results. `Stop` uploads the full transcript.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `EVENT_STORE_ENABLED` | `true` | Master switch for the event store |
| `EVENT_STORE_BACKEND` | `filesystem` | Storage backend: `filesystem` or `s3` |
| `EVENT_STORE_PATH` | `~/.intaris/events` | Filesystem backend path |
| `EVENT_STORE_FLUSH_SIZE` | `100` | Events per chunk before flushing |
| `EVENT_STORE_FLUSH_INTERVAL` | `30` | Seconds between periodic flushes |
| `EVENT_STORE_S3_*` | (various) | S3 endpoint, bucket, credentials |

### UI Player

The session recording player is embedded in the Sessions tab's session detail expansion. Features:
- Scrollable event list with type badges and expandable JSON details
- Event type filtering
- Live tailing via WebSocket (`session_event` type)
- Play/pause mode with configurable speed (0.5x-10x)
- Pagination (load more on demand)

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
- **Shutdown timeout budget**: Each cleanup step has a timeout to prevent hanging on unresponsive services. MCP proxy: 5s, background worker: 5s, webhook client: 2s, notification dispatcher: 2s. Worst case total: ~14s. Kubernetes `terminationGracePeriodSeconds` should be set to at least 20s.

## Integrations

Client integrations live in `integrations/` and provide two approaches for each tool:

1. **REST API plugin** (recommended): Intercepts tool calls in the client and evaluates them via `POST /api/v1/evaluate`. Gives fine-grained control over error messages, fail-open/fail-closed behavior, and session lifecycle.
2. **MCP proxy**: Configures the client to point at Intaris's `/mcp` endpoint. Zero code — just configuration. Full MCP proxy features (tool preferences, escalation retry, namespacing).

**Do not use both approaches simultaneously** — tool calls would be evaluated twice.

### OpenCode

- **Plugin**: `integrations/opencode/intaris.ts` — TypeScript plugin using `tool.execute.before` hook. Creates Intaris sessions on `session.created` (including child sessions with `parent_session_id`), evaluates every tool call before execution, sends periodic checkpoints, and signals session completion on `session.deleted`.
- **MCP config**: `integrations/opencode/opencode.json` — Remote MCP server pointing at `/mcp`.
- **Env vars**: `INTARIS_URL`, `INTARIS_API_KEY`, `INTARIS_AGENT_ID` (default: `opencode`), `INTARIS_USER_ID`, `INTARIS_FAIL_OPEN` (default: `false`), `INTARIS_INTENTION`, `INTARIS_ALLOW_PATHS` (comma-separated parent dirs for cross-project reads, e.g., `~/src`), `INTARIS_CHECKPOINT_INTERVAL` (default: `25`, `0`=disabled).
- **Install**: Copy `intaris.ts` to `~/.config/opencode/plugins/` (global) or `.opencode/plugins/` (project).
- **OpenCode permissions**: Set `"permission": "allow"` in `opencode.jsonc` to disable OpenCode's built-in approval prompts and let Intaris be the sole gatekeeper. See [OpenCode Permissions](https://opencode.ai/docs/permissions/).
- **Behavioral analysis**: Tracks per-session statistics (call count, approve/deny/escalate breakdown, recent tools). Sends periodic checkpoints via `POST /checkpoint`. Signals completion via `PATCH /session/{id}/status` + `POST /session/{id}/agent-summary` on session deletion.

### Claude Code

- **Hooks**: `integrations/claude-code/hooks.json` — `SessionStart` creates session, `PreToolUse` evaluates tool calls and sends periodic checkpoints, `Stop` signals session completion and sends agent summary.
- **Scripts**: `integrations/claude-code/scripts/session.sh`, `evaluate.sh`, and `stop.sh` — Bash scripts using `curl` and `jq`. Session state tracked as JSON in `/tmp/intaris_state_*.json`.
- **Env vars**: Same as OpenCode, plus `INTARIS_ALLOW_PATHS` (comma-separated parent dirs for cross-project reads), `INTARIS_DEBUG` (default: `false`) for stderr logging.
- **Install**: Copy scripts to `~/.claude/scripts/`, merge `hooks.json` into `~/.claude/settings.json`.
- **Behavioral analysis**: Tracks per-session statistics in JSON state files. Sends periodic checkpoints via `POST /checkpoint`. Signals completion via `PATCH /session/{id}/status` + `POST /session/{id}/agent-summary` on session stop.

### Tool name conventions

Different clients use different tool naming conventions:

| Client | Built-in tools | MCP tools |
|---|---|---|
| **OpenCode** | `read`, `edit`, `write`, `bash` | `server_tool` (single underscore, e.g., `mnemory_add_memory`) |
| **Claude Code** | `Read`, `Edit`, `Write`, `Bash` (capitalized) | `mcp__server__tool` (double underscore, e.g., `mcp__mnemory__add_memory`) |
| **Intaris MCP proxy** | N/A | `server_name:tool_name` (colon, e.g., `mnemory:add_memory`) |

Session policies (fnmatch patterns) must use the naming convention of the integration approach being used.
