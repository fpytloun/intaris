# Architecture

Intaris is a guardrails service that sits between AI agents and their tools. Every tool call passes through a classification and evaluation pipeline before execution is allowed.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Clients                                                        │
│  ┌──────────┐  ┌────────────┐  ┌──────────────────────────┐    │
│  │ OpenCode │  │ Claude Code│  │ Any MCP Client           │    │
│  │ (plugin) │  │  (hooks)   │  │ (Cursor, Cline, etc.)    │    │
│  └────┬─────┘  └─────┬──────┘  └────────────┬─────────────┘    │
│       │               │                      │                  │
└───────┼───────────────┼──────────────────────┼──────────────────┘
        │ REST API      │ REST API             │ MCP Protocol
        ▼               ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  Intaris Server (Starlette + FastAPI)                           │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Transport Layer                                        │    │
│  │  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───────┐ │    │
│  │  │ REST API │  │ WebSocket │  │ MCP Proxy│  │Health │ │    │
│  │  │ /api/v1  │  │ /stream   │  │ /mcp     │  │/health│ │    │
│  │  └────┬─────┘  └─────┬─────┘  └────┬─────┘  └───────┘ │    │
│  └───────┼───────────────┼─────────────┼───────────────────┘    │
│          │               │             │                        │
│  ┌───────┼───────────────┼─────────────┼───────────────────┐    │
│  │  Core Pipeline        │             │                   │    │
│  │       ▼               │             ▼                   │    │
│  │  ┌──────────┐    ┌────┴────┐   ┌──────────┐            │    │
│  │  │Classifier│    │EventBus │   │MCP Client│            │    │
│  │  └────┬─────┘    └─────────┘   │ Manager  │            │    │
│  │       ▼                        └──────────┘            │    │
│  │  ┌──────────┐                                          │    │
│  │  │   LLM    │ ◄── Intention + Alignment Barriers       │    │
│  │  │Evaluator │                                          │    │
│  │  └────┬─────┘                                          │    │
│  │       ▼                                                │    │
│  │  ┌──────────┐                                          │    │
│  │  │ Decision │                                          │    │
│  │  │ Matrix   │                                          │    │
│  │  └────┬─────┘                                          │    │
│  └───────┼────────────────────────────────────────────────┘    │
│          │                                                      │
│  ┌───────┼────────────────────────────────────────────────┐    │
│  │  Storage & Background                                  │    │
│  │       ▼                                                │    │
│  │  ┌────────┐  ┌───────┐  ┌───────────┐  ┌──────────┐   │    │
│  │  │ SQLite │  │ Audit │  │Event Store│  │Background│   │    │
│  │  │Sessions│  │  Log  │  │  (ndjson) │  │ Worker   │   │    │
│  │  └────────┘  └───────┘  └───────────┘  └──────────┘   │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Management UI (Alpine.js + Tailwind CSS)               │    │
│  │  Dashboard │ Sessions │ Audit │ Approvals │ Servers     │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## Layer Responsibilities

| Layer | Module | Responsibility |
|---|---|---|
| **Transport** | `server.py` | HTTP routing, auth middleware (ContextVars), health endpoint, lifespan init |
| **Identity** | `api/deps.py` | SessionContext dependency (user_id, agent_id from ContextVars) |
| **REST API** | `api/` | FastAPI endpoints with OpenAPI spec |
| **Streaming** | `api/stream.py` | EventBus (pub/sub) + WebSocket endpoint with first-message auth |
| **Info** | `api/info.py` | Identity (/whoami), stats (/stats), config (/config) for management UI |
| **Intention** | `intention.py` | IntentionBarrier (user-driven intention updates) + generate_intention() |
| **Orchestration** | `evaluator.py` | Full evaluation pipeline (classify -> LLM -> decide -> audit), behavioral context |
| **Alignment** | `alignment.py` | AlignmentBarrier (parent/child intention enforcement via LLM) |
| **Classification** | `classifier.py` | Read-only allowlist, critical patterns, session policy, path policy |
| **Decision** | `decision.py` | Priority-ordered decision matrix |
| **LLM** | `llm.py` | OpenAI-compatible client with structured output |
| **Prompts** | `prompts.py` | Safety evaluation prompt templates |
| **Analysis Prompts** | `prompts_analysis.py` | L2/L3 analysis prompt templates + JSON schemas (window + compaction) |
| **Analyzer** | `analyzer.py` | L2 hierarchical summary generation (window + compaction) + L3 agent-scoped cross-session analysis |
| **Background** | `background.py` | TaskQueue (SQLite-backed), BackgroundWorker (idle sweep, scheduler), Metrics |
| **Redaction** | `redactor.py` | Secret redaction before audit storage |
| **Rate Limiting** | `ratelimit.py` | In-memory sliding window rate limiter per (user_id, session_id) |
| **Webhook** | `webhook.py` | Async webhook client with HMAC-SHA256 signing |
| **Event Store** | `events/` | Session recording: chunked ndjson storage, write buffering |
| **MCP Proxy** | `mcp/` | Upstream MCP connections, tool aggregation, call routing |
| **Session** | `session.py` | Session CRUD, counter management, paginated listing, idle sweep |
| **Audit** | `audit.py` | Audit log CRUD and querying |
| **Database** | `db.py` | SQLite connection management, schema, migrations |
| **Configuration** | `config.py` | Environment variable parsing into dataclasses |

## Key Design Decisions

### Default-Deny Classifier

The classifier uses an explicit read-only allowlist. Only tools and commands that are provably read-only (e.g., `grep`, `cat`, `git status`) are auto-approved. Everything else -- including unknown tools, third-party MCP tools, and unrecognized bash commands -- goes through LLM evaluation.

This is the opposite of a blocklist approach. New tools are safe by default because they require evaluation, not because someone remembered to add them to a deny list.

### Priority-Ordered Decision Matrix

The decision matrix applies rules in strict priority order:

| Priority | Condition | Decision |
|---|---|---|
| 1 | Critical risk (any alignment) | **Deny** |
| 2 | LLM explicitly said "deny" | **Deny** |
| 3 | Aligned + low risk | **Approve** |
| 4 | Aligned + medium risk | **Approve** |
| 5 | Aligned + high risk | **Escalate** |
| 6 | Not aligned (any risk) | **Escalate** |

### 5-Second Circuit Breaker

Client integrations (OpenCode plugin, Claude Code hooks) have a 5-second timeout for evaluation calls. The LLM timeout defaults to 4000ms to ensure Intaris responds within this window. Read-only fast-path decisions resolve in under 1ms.

### User-Driven Intention Model

Session intention is immutable except by user action. Agent tool calls never redefine intention. The `IntentionBarrier` coordinates between the `/reasoning` endpoint (which receives user messages) and the `/evaluate` endpoint (which waits for pending intention updates) to ensure the evaluator always sees the freshest user-stated intention.

### Multi-Tenancy

`user_id` is the tenant separator. Every session and audit record is scoped to a `user_id`, and all database queries include a `WHERE user_id = ?` clause. `agent_id` is metadata only -- the human operator sees all their sessions across all agents.

## Session Lifecycle

Sessions progress through these states:

```
                    ┌──────────┐
                    │  active  │◄──────────────────────┐
                    └────┬─────┘                       │
                         │                             │
              idle timeout (30 min)            evaluate call
                         │                     (auto-resume)
                         ▼                             │
                    ┌──────────┐                       │
                    │   idle   │───────────────────────┘
                    └────┬─────┘
                         │
                    user/agent action
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
      ┌──────────┐ ┌──────────┐ ┌────────────┐
      │completed │ │suspended │ │terminated  │
      └──────────┘ └──────────┘ └────────────┘
```

- **active**: Normal operation. Tool calls are evaluated.
- **idle**: No activity for `SESSION_IDLE_TIMEOUT_MINUTES` (default 30). Auto-resumed on next evaluate call.
- **completed**: Session finished normally. All evaluations denied.
- **suspended**: Paused by user or alignment violation. All evaluations denied.
- **terminated**: Hard-killed. All evaluations denied.

### Parent/Child Sessions

Sessions can form hierarchies. When a client spawns a subagent (e.g., OpenCode's `@explore` or `@plan` tasks), it creates a child session with `parent_session_id`. The `AlignmentBarrier` enforces that child intentions are compatible with the parent's intention via LLM check.

If the parent session is terminated or suspended, child sessions are automatically suspended.

## Intention Model

### IntentionBarrier

Coordinates real-time intention updates between user messages and tool evaluations:

1. `POST /reasoning` receives a user message -> triggers async LLM intention regeneration
2. `POST /evaluate` calls `await barrier.wait()` -> blocks up to 1s if an update is pending
3. New user messages cancel and restart the update (only the latest runs to completion)

### AlignmentBarrier

Enforces parent/child session intention compatibility:

1. Child session created with `parent_session_id` -> triggers async LLM alignment check
2. First `POST /evaluate` waits up to 15s for the check to complete
3. Misalignment -> escalation (not suspension), so the user can approve via the UI
4. User acknowledgment persisted via `alignment_overridden` flag

### One-Time Bootstrap

Sessions that never receive user messages (e.g., MCP proxy sessions) keep their generic initial intention. At evaluate call 10, if `intention_source` is still `"initial"`, a single refinement fires via the background task queue. This is capped at exactly one update to prevent agent drift.

## Behavioral Analysis

Three-layer behavioral guardrails system with hierarchical session support:

| Layer | Scope | Data Source | Output |
|---|---|---|---|
| **L1** | Per-call | `/reasoning`, `/checkpoint`, `/evaluate` | Raw data collection |
| **L2** | Per-session | Background worker triggers | Window summaries + compacted session summaries |
| **L3** | Per-user | Periodic cross-session analysis | Behavioral risk profiles (root sessions only) |

Agent-reported data (reasoning, checkpoints, summaries) is stored but never included in Intaris analysis prompts -- it's kept for post-hoc comparison only.

### Hierarchical Sessions

Parent sessions incorporate child session data into their summaries. The flow:

1. **Child sessions** get independent L2 window summaries (unchanged from flat sessions)
2. **Parent sessions** collect child data (compacted > window > raw metadata fallback) and include it in both window and compaction prompts
3. **Summary compaction** synthesizes multiple window summaries into a single session-level assessment with supersede semantics (old compacted deleted, new one inserted)
4. **L3 analysis** operates only on root sessions (`parent_session_id IS NULL`) -- child data is already embedded in parent compacted summaries

Task-queue orchestration ensures children are summarized before parents: parent tasks check for unsummarized children, enqueue child tasks at higher priority, and re-enqueue themselves with a 30s delay (max 5 re-enqueues).

### Budget-Aware Partitioning

Both the event-enriched path and the audit_log-only fallback path use budget-aware partitioning -- no content is ever silently dropped. When data exceeds the window budget (150k chars, configurable via `ANALYSIS_WINDOW_CHARS`), more windows are created, each getting its own LLM call. Per-entry safety valves at generous limits (e.g., 5k chars for reasoning, 2k for WRITE args content) ensure individual entries don't dominate the budget, with explicit truncation metadata appended when triggered.

Tool call arguments are summarized differently by classification: WRITE/CRITICAL operations include up to 2000 chars of content with content security flags; READ operations stay brief. File paths are never truncated.

Content security scanning (`_scan_content_flags`) provides defense-in-depth by scanning full, untruncated tool args for dangerous patterns (SSH access, code execution, credential access, etc.) and appending compact flag strings to prompt lines.

### L3 Cross-Session Analysis

L3 uses a **separate LLM configuration** (`ANALYSIS_L3_LLM_*` env vars) -- typically a more capable model (default `gpt-5.4`) with its own context budget (`ANALYSIS_L3_WINDOW_CHARS`, default 200k chars). L3 uses **progressive summarization** to stay within budget: recent sessions (last 3 days) get full summary narratives, older sessions get compressed structured metadata only.

Compaction prompts include cross-window aggregate metrics (total denial/escalation rates, alignment trajectory, indicator frequency) to surface distributed patterns invisible at the per-window level.

### Risk Indicators

Risk indicators flag concerning behavioral patterns: `intent_drift`, `restriction_circumvention`, `scope_creep`, `insecure_reasoning`, `unusual_tool_pattern`, `injection_attempt`, `escalation_pattern`, and `delegation_misalignment` (sub-session divergence from parent intention).

## Database

SQLite with WAL mode for concurrent read/write. Thread-local connections. Foreign keys enabled.

Key tables: `sessions`, `audit_log`, `mcp_servers`, `mcp_tool_preferences`, `session_summaries`, `agent_summaries`, `behavioral_analyses`, `behavioral_profiles`, `analysis_tasks`, `notification_channels`.

Sessions use compound PK `(user_id, session_id)` for tenant isolation. Audit log uses compound FK `(user_id, session_id)` referencing sessions. The `session_summaries` table has a `summary_type` column (`window` or `compacted`) and a `trigger` column that includes `compaction` as a valid value. Index `idx_sessions_parent` on `sessions(user_id, parent_session_id)` supports efficient child session lookups for hierarchical analysis.
