# Development

Guide for contributing to Intaris.

## Setup

```bash
# Clone the repository
git clone https://github.com/fpytloun/intaris.git
cd intaris

# Install with dev dependencies
uv pip install -e ".[dev]"

# Or using make
make dev
```

## Running Locally

```bash
export LLM_API_KEY=sk-your-key
uv run intaris
```

The server starts at `http://localhost:8060`. The management UI is at `http://localhost:8060/ui`.

## Tests

### Unit Tests

Fast tests that don't require an LLM API key:

```bash
uv run pytest tests/ -v

# Or using make
make test
```

### End-to-End Tests

E2E tests use real LLM calls to verify the full evaluation pipeline. They require `LLM_API_KEY` or `OPENAI_API_KEY`:

```bash
uv run pytest -m e2e -v
uv run pytest -m e2e -v --timeout=60  # with generous timeout
```

### All Tests

```bash
uv run pytest -m '' -v

# Or using make
make test-all
```

### Test Structure

```
tests/
├── test_alignment.py     # AlignmentBarrier tests
├── test_api.py           # REST API endpoint tests
├── test_background.py    # Background worker/task queue tests
├── test_classifier.py    # Tool classification tests
├── test_config.py        # Configuration tests
├── test_db.py            # Database tests
├── test_decision.py      # Decision matrix tests
├── test_e2e.py           # End-to-end tests (requires LLM API key)
├── test_events.py        # Event store tests
├── test_intention.py     # IntentionBarrier tests
├── test_mcp.py           # MCP proxy tests
├── test_path_policy.py   # Filesystem path protection tests
├── test_ratelimit.py     # Rate limiter tests
├── test_redactor.py      # Secret redaction tests
├── test_stream.py        # WebSocket/EventBus tests
└── test_webhook.py       # Webhook client tests
```

### E2E Test Categories (30 tests)

| Category | Tests | Description |
|---|---|---|
| Fast paths | 2 | Read-only auto-approve, critical auto-deny |
| Aligned approval | 5 | Clearly aligned tool calls -> approve via LLM |
| Misaligned | 5 | Misaligned tool calls -> deny or escalate via LLM |
| Dangerous operations | 4 | Malicious operations -> strict deny |
| Same tool, different context | 2 | Same command, different intention -> different outcome |
| Session lifecycle | 3 | Status enforcement, counter accuracy |
| Audit trail | 3 | Record creation, secret redaction, filtering |
| Escalation workflow | 2 | Escalation -> resolution -> audit verification |
| Session policy | 2 | Policy allow/deny overrides classification |
| High risk | 2 | Aligned but risky operations |

## Linting and Formatting

```bash
# Check for issues
uv run ruff check intaris/ tests/

# Auto-format
uv run ruff format intaris/ tests/

# Or using make
make lint
make format
```

Configuration in `pyproject.toml`:
- Target: Python 3.11
- Line length: 88
- Rules: E (pycodestyle errors), F (pyflakes), I (isort), W (pycodestyle warnings)
- E501 (line too long) is ignored

## Makefile Targets

| Target | Description |
|---|---|
| `make dev` | Install with dev dependencies |
| `make test` | Run unit tests |
| `make test-all` | Run unit + e2e tests |
| `make lint` | Ruff lint check |
| `make format` | Ruff auto-format |
| `make css` | Build minified Tailwind CSS |
| `make css-watch` | Tailwind watch mode |
| `make clean` | Remove build artifacts |

## Code Conventions

### Style

- Python 3.11+ features (type unions with `|`, `from __future__ import annotations`)
- Type hints on all function signatures
- Docstrings on all public classes and methods
- `logging` module for all output (never `print()`)
- f-strings for string formatting
- All code comments and identifiers in English

### Error Handling

- API endpoints catch `ValueError` (-> 4xx) and `Exception` (-> 500)
- Internal errors logged with `logger.exception()` for stack traces
- Evaluator propagates LLM failures as exceptions (-> 500), letting clients retry

### Configuration

- All config via environment variables (no config files except MCP server definitions)
- Dataclass-based config objects in `config.py`
- `load_config()` validates required fields at startup
- Defaults optimized for local development

### Database

- SQLite with WAL mode for concurrent read/write
- Thread-local connections via `threading.local()`
- Foreign keys enabled
- Sessions use compound PK `(user_id, session_id)` for tenant isolation
- Audit log uses compound FK `(user_id, session_id)` referencing sessions

### Redaction

- `redact()` always returns a deep copy -- never mutates input args
- Pattern-based: API keys, passwords, connection strings, JWTs, private keys
- Key-name-based: Any key containing `password`, `token`, `secret`, `key`, `credential`, `auth`

### Session Policy

- Uses fnmatch glob patterns (NOT regex) for custom allow/deny rules to prevent ReDoS attacks

## Rebuilding Tailwind CSS

After modifying UI files:

```bash
# One-time build
make css

# Watch mode (auto-rebuild on changes)
make css-watch
```

The pre-built `app.css` is committed to the repository. Always rebuild and commit after UI changes.

## Project Structure

```
intaris/
├── server.py              # HTTP server, auth middleware, lifespan, MCP mount
├── config.py              # Environment variable configuration
├── evaluator.py           # Evaluation pipeline orchestrator
├── classifier.py          # Tool call classification
├── decision.py            # Decision matrix
├── llm.py                 # OpenAI-compatible LLM client
├── prompts.py             # Safety evaluation prompts
├── prompts_analysis.py    # Analysis prompt templates
├── intention.py           # IntentionBarrier + generate_intention()
├── alignment.py           # AlignmentBarrier
├── session.py             # Session CRUD
├── audit.py               # Audit log storage
├── analyzer.py            # L2 session summaries + L3 cross-session analysis
├── background.py          # Task queue + background worker
├── redactor.py            # Secret redaction
├── ratelimit.py           # Rate limiter
├── webhook.py             # Webhook client
├── crypto.py              # Fernet encryption
├── db.py                  # SQLite management
├── api/                   # FastAPI endpoints
│   ├── evaluate.py        # POST /evaluate
│   ├── intention.py       # Session CRUD endpoints
│   ├── audit.py           # Audit + decision endpoints
│   ├── info.py            # Stats, config, whoami
│   ├── mcp.py             # MCP server management
│   ├── analysis.py        # Behavioral analysis endpoints
│   ├── notifications.py   # Notification channel management
│   ├── events.py          # Session recording endpoints
│   ├── stream.py          # WebSocket streaming
│   ├── actions.py         # Token-based approve/deny actions
│   ├── deps.py            # SessionContext dependency
│   └── schemas.py         # Pydantic models
├── events/                # Event store
│   ├── backend.py         # Filesystem + S3 backends
│   └── store.py           # EventStore with buffering
├── mcp/                   # MCP proxy
│   ├── store.py           # Server config CRUD
│   ├── config.py          # File-based config loader
│   ├── client.py          # Upstream connection manager
│   └── proxy.py           # MCP server handlers
├── notifications/         # Notification system
│   ├── dispatcher.py      # Notification dispatcher
│   ├── providers.py       # Pushover, Slack, webhook providers
│   ├── store.py           # Channel storage
│   └── tokens.py          # Action token management
└── ui/                    # Management UI
    ├── static/            # Pre-built frontend
    └── src/               # Tailwind source CSS
```
