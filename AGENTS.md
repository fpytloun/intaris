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
├── server.py              # HTTP server entry point, health endpoint, auth middleware
├── config.py              # Configuration from environment variables (dataclasses)
├── db.py                  # SQLite connection management, table creation, indexes
├── session.py             # Session CRUD + counter updates
├── audit.py               # Audit log storage + querying
├── classifier.py          # Tool call classification (read/write/critical)
├── redactor.py            # Secret redaction for audit args
├── llm.py                 # OpenAI-compatible LLM client with structured output
├── prompts.py             # Safety evaluation prompt templates + JSON schema
├── evaluator.py           # Evaluation pipeline orchestrator
├── decision.py            # Decision matrix (priority-ordered)
├── api/
│   ├── __init__.py        # FastAPI sub-app factory
│   ├── deps.py            # SessionContext dependency (identity from ContextVars)
│   ├── schemas.py         # Pydantic request/response models
│   ├── evaluate.py        # POST /api/v1/evaluate
│   ├── intention.py       # POST /api/v1/intention, GET /api/v1/session/{id}
│   └── audit.py           # GET /api/v1/audit, POST /api/v1/decision
└── ui/                    # Built-in UI (Phase 1 Week 3)
    └── static/
```

### Layer responsibilities

| Layer | File | Responsibility |
|---|---|---|
| **Transport** | `server.py` | HTTP routing, auth middleware (ContextVars), health endpoint |
| **Identity** | `api/deps.py` | SessionContext dependency (user_id, agent_id from ContextVars) |
| **REST API** | `api/` | FastAPI endpoints with OpenAPI spec |
| **Orchestration** | `evaluator.py` | Full evaluation pipeline (classify → LLM → decide → audit) |
| **Classification** | `classifier.py` | Read-only allowlist, critical patterns, session policy |
| **Decision** | `decision.py` | Priority-ordered decision matrix |
| **LLM** | `llm.py` | OpenAI-compatible client with structured output |
| **Prompts** | `prompts.py` | Safety evaluation prompt templates |
| **Redaction** | `redactor.py` | Secret redaction before audit storage |
| **Session** | `session.py` | Session CRUD and counter management |
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

## Important Notes

- **Evaluation pipeline**: classify → critical check → LLM → decision matrix → audit. Fast path skips LLM for read-only and critical classifications.
- **LLM timeout**: Configured via `LLM_TIMEOUT_MS` (default 4000ms). Must be under the 5-second circuit breaker in the Executor Adapter.
- **Session policy**: Uses fnmatch glob patterns (NOT regex) for custom allow/deny rules to prevent ReDoS attacks.
- **Redaction immutability**: `redact()` always returns a deep copy. Never mutates input args.
