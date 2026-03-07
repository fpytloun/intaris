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
| **Transport** | `server.py` | HTTP routing, auth middleware, health endpoint |
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
- Indexes on `audit_log(session_id, timestamp)` and `audit_log(decision)`

## Important Notes

- **Evaluation pipeline**: classify → critical check → LLM → decision matrix → audit. Fast path skips LLM for read-only and critical classifications.
- **LLM timeout**: Configured via `LLM_TIMEOUT_MS` (default 4000ms). Must be under the 5-second circuit breaker in the Executor Adapter.
- **Session policy**: Uses fnmatch glob patterns (NOT regex) for custom allow/deny rules to prevent ReDoS attacks.
- **Redaction immutability**: `redact()` always returns a deep copy. Never mutates input args.
