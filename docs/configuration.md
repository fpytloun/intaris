# Configuration Reference

## Cognis-managed inference

When managed inference is enabled, unset `LLM_REASONING_EFFORT`,
`ANALYSIS_LLM_REASONING_EFFORT`, `ANALYSIS_L3_LLM_REASONING_EFFORT` and
`JUDGE_LLM_REASONING_EFFORT` to inherit Cognis's routing defaults. Standalone
implicit `low` defaults are not sent in managed mode. Explicit values remain
client overrides; L3 continues to inherit an explicitly configured analysis
effort unless its own variable is set. Empty values omit effort.

Set `COGNIS_INFERENCE_URL` to Cognis's `/v1` endpoint and
`COGNIS_INFERENCE_TOKEN_FILE` to a protected file containing an Intaris inference
service JWT. In this mode Cognis's admin-configured global routes select
evaluation, judge, analysis, deep-analysis and embedding models. Direct LLM
settings remain the default when these variables are absent.

### Evaluator backend follows the Cognis route (`EVALUATOR_BACKEND=auto`)

Backend selection shares one process-local metadata refresh between synchronous
and asynchronous callers. The metadata worker is created only on a cache miss,
uses one thread, and retains the configured `LLM_TIMEOUT_MS` HTTP timeout.
Async HTTP and MCP evaluations suspend at their existing selection sites without
occupying default-executor slots; metadata-independent fast paths do not fetch
metadata. Cancellation detaches only that evaluation, not the shared refresh.
Invalidation discards an in-flight result before another refresh can begin.
Shutdown drains the metadata worker before closing its HTTP client.

`EVALUATION_DEADLINE_MS` (default `60000`) is the shared model-retry budget,
not a hard endpoint cancellation deadline or a metadata timeout. Suspending and
resuming an evaluation does not reset that budget.

With managed inference and `EVALUATOR_BACKEND` unset (or set to `auto`), the
evaluator follows the model selected for Cognis's `intaris_evaluation` route.
Switching between an LLM and the experimental Jev evaluator only requires
changing that route in Cognis; no Intaris environment change or restart.

- Intaris reads `GET /v1/models` with its companion token and looks at
  `routes/intaris_evaluation` → `cognis.operations`. `["systemone"]` selects
  Jev (`POST /v1/systemone`); `["chat"]` selects the LLM evaluator.
- The result is cached per process for about 60 seconds. Concurrent lookups
  are coalesced. Lookups happen only when an evaluation needs a model, never
  once per evaluation. A failed lookup is retried after about 5 seconds.
- During a switch, Cognis rejects a request that uses the old protocol before
  upstream dispatch (`400 invalid_model`). Evaluations that already selected
  the old backend fail closed (normally one; more if several were in flight),
  the cache is cleared, and later evaluations use the new backend. Intaris
  does not replay the call or infer the backend from model names. With a
  pinned `llm`/`jev`, `/api/v1/config` shows the latest rejection in
  `evaluator.backend_error` for about 60 seconds.
- Older Cognis without `cognis.operations`: Intaris uses the LLM evaluator and
  logs this once. Malformed or unknown metadata (for example both `chat` and
  `systemone`, or a missing route) fails evaluations closed.
- Judge, intention generation, and L2/L3 analysis always use their chat routes.
  Parent/child alignment uses the selected evaluator, as before.
- In managed mode `JEV_MODEL`, `JEV_BASE_URL`, and standalone Jev keys are
  replaced by the Cognis route, endpoint, and rotating companion token. No
  TypeSafe/OpenRouter key is needed. Confidence thresholds and decision policy
  are unchanged.

Explicit `EVALUATOR_BACKEND=llm` or `jev` pins the backend and keeps its
meaning. If the route does not match the pin, each evaluation fails closed
with an error that names the pinned mode; Intaris does not switch.
`EVALUATOR_BACKEND=auto` without `COGNIS_INFERENCE_URL` is a startup error.
Without managed inference the default remains `llm`, and standalone Jev
(`EVALUATOR_BACKEND=jev` with `JEV_API_KEY`) is unchanged.

Troubleshooting: `GET /api/v1/config` reports `evaluator.mode`, the active
`evaluator.backend` (`llm`, `jev`, or `null`), and `evaluator.backend_error`.
Look for `Evaluator backend selected`, `older Cognis`, `metadata rejected`,
or `rejected the ... protocol` in the Intaris logs.

The token file is read for each request, not at startup; atomic rotation does
not require restart. Missing credentials or unconfigured Cognis routing leave
inference unavailable and evaluation fail-closed. JWT verification for incoming
Intaris API requests remains a separate configuration. Keep search dimensions
consistent with the global embedding route, and reindex before changing models.

All settings are loaded from environment variables. Defaults are optimized for local development -- just set `LLM_API_KEY` and run.

Data is stored in `~/.intaris` by default (override with `DATA_DIR`). In Docker, `DATA_DIR` is set to `/data` for volume mounting.

### Evaluation and review timing

`EVALUATION_DEADLINE_MS` defaults to **60000**. This is the shared request
ceiling for L1, bounded Judge repair and independent authorization checks.
Individual model-call timeouts still apply. Evaluate clients must allow
transport headroom above this ceiling; Cognis uses 65 seconds only for
`POST /api/v1/evaluate` (other endpoint timeouts are unchanged).

`LLM_TIMEOUT_MS` and `JEV_TIMEOUT_MS` bound each transport attempt, not the
entire retry sequence when a shared evaluation deadline exists. A timed-out
attempt may retry within the remaining shared budget and configured attempt
limit. Standalone calls without an outer deadline retain a total budget equal
to their configured timeout. For slower routed inference, configure suitable
per-attempt limits (for example 15000 ms) without extending the shared ceiling.
Malformed responses and authentication failures are not made retryable.

An incomplete hybrid review returns HTTP **503**, code `review_unavailable`,
with `retryable: true`. No tool is dispatched and no human-permission
notification is emitted for that infrastructure failure. A completed reviewer
request for a human decision still follows normal escalation handling.
Qualification must use the deployed model/request deadlines, not minute-scale
benchmark-only overrides.

## Server

| Variable | Default | Description |
|---|---|---|
| `INTARIS_HOST` | `0.0.0.0` | HTTP server bind address |
| `INTARIS_PORT` | `8060` | HTTP server port |
| `METRICS_ENABLED` | `true` | Start the dedicated unauthenticated metrics listener |
| `METRICS_HOST` | `0.0.0.0` | Metrics listener bind address |
| `METRICS_PORT` | `9090` | Dedicated Prometheus metrics port; must differ from `INTARIS_PORT` |
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

## Tool-Call Evaluator

| Variable | Default | Description |
|---|---|---|
| `EVALUATOR_BACKEND` | `auto` with managed inference, else `llm` | Evaluator backend: `llm`, `jev`, or `auto` (follow the Cognis `intaris_evaluation` route; managed inference only). |
| `EVALUATION_DEADLINE_MS` | `60000` | Shared model-call retry budget from `/evaluate` arrival, including L1, repairs and the Judge. Not a hard endpoint deadline or metadata timeout. |
| `LLM_API_KEY` | (required for generative features) | API key for the LLM provider. Falls back to `OPENAI_API_KEY`. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | LLM API base URL. Falls back to `OPENAI_API_BASE`. |
| `LLM_MODEL` | `gpt-5.4-nano` | Model for safety evaluation when `EVALUATOR_BACKEND=llm`. |
| `LLM_REASONING_EFFORT` | `low` | Reasoning effort hint (provider-specific). |
| `LLM_TIMEOUT_MS` | `4000` | Total budget per LLM generation, including transient retries and format repairs. Also limits each attempt. Minimum: 500ms. |
| `JEV_API_KEY` | (required for Jev) | Jev provider API key. Falls back to `TYPESAFE_API_KEY` only for the native TypeSafe endpoint; OpenRouter requires an explicit key. |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | TypeSafe API base URL; set `https://openrouter.ai/api` for OpenRouter's decisions API. |
| `JEV_MODEL` | `jev-1.13.0` | Pinned Jev model; OpenRouter: `typesafe/jev-1.13-20260917`. |
| `JEV_TIMEOUT_MS` | `4000` | Total budget per Jev request, including transient retries. Also limits each attempt. Minimum: 500ms. |
| `JEV_MINIMUM_CONFIDENCE` | `0.6` | Minimum confidence for each Jev classification dimension. Lower-confidence tool calls and alignments escalate. |
| `JEV_DECISION_QUESTION` | `true` | Keep the third Jev disposition question; set `false` only for the experimental two-axis benchmark, where Intaris derives disposition from alignment and risk. |
| `JEV_DIAGNOSTICS` | `false` | Include model probabilities, per-question confidence, and token usage in authenticated evaluation responses for calibration. No tool arguments or intention text. |
| `JEV_APPROVAL_RISK_CONFIDENCE` | (unset) | Experimental risk-confidence floor for aligned low/medium-risk calls when `JEV_DECISION_QUESTION=false`; alignment and all other risks retain `JEV_MINIMUM_CONFIDENCE`. Requires explicit calibration. |

### Model Selection

The evaluation model should be fast and inexpensive -- it is called on every
non-read-only tool call. The default `llm` backend requires an
OpenAI-compatible model with structured JSON output.

The `jev` backend uses TypeSafe's System One API for bounded alignment, risk,
and disposition classifications. It also handles parent/child intention
alignment. Jev does not replace generative features: intention generation,
the judge, L2 summaries and L3 analysis continue using their respective LLM
configuration. Jev provider failures propagate as evaluation failures; Intaris
does not silently switch models. Jev's returned probabilities are converted
into concise audit text and are not represented as model-generated reasoning.
It is opt-in and **not the recommended production default**; see the
[Jev evaluator assessment](jev-evaluator.md) for benchmark results and caveats.

Example split configuration:

```bash
EVALUATOR_BACKEND=jev
JEV_API_KEY=...
JEV_MODEL=jev-1.13.0

# Generative intention, analysis and judge paths
LLM_API_KEY=...
ANALYSIS_LLM_MODEL=gpt-5.4-mini
JUDGE_LLM_MODEL=gpt-5.4
```

For Jev through OpenRouter with Groq-backed generative models:

```bash
EVALUATOR_BACKEND=jev
JEV_BASE_URL=https://openrouter.ai/api
JEV_MODEL=typesafe/jev-1.13-20260917
JEV_API_KEY=<OpenRouter API key>
LLM_MODEL=gpt-oss-20b
LLM_BASE_URL=https://api.groq.com/openai/v1
ANALYSIS_LLM_MODEL=gpt-oss-120b
JUDGE_LLM_MODEL=gpt-oss-120b
```

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
| `EVENT_STORE_COMPRESSION` | `zstd` | New archive chunks: checksummed Zstandard level 1 (`.ndjson.zst`), or `none` for plain NDJSON |
| `EVENT_STORE_PATH` | `~/.intaris/events` | Filesystem backend storage path |
| `EVENT_STORE_SPOOL_ENABLED` | `true` | Stage local pending batches before acknowledging; one writer/replica only |
| `EVENT_STORE_SPOOL_FSYNC` | `true` | Sync spool files/directories; disable only when host-crash durability is not required |
| `EVENT_STORE_SPOOL_PATH` | `${EVENT_STORE_PATH}-spool` | Separate local spool directory; default `~/.intaris/events-spool`, including with S3 |
| `EVENT_STORE_SPOOL_MAX_BYTES` | `1073741824` | Admission budget including working headroom; full spool stops writes, never evicts |
| `EVENT_STORE_BACKEND_CHECK_INTERVAL` | `5` | Seconds between backend probes; a failed probe/read/upload stops admission until recovery |
| `EVENT_STORE_FLUSH_SIZE` | `100` | Events per chunk before flushing |
| `EVENT_STORE_FLUSH_BYTES` | `4194304` | Approximate serialized bytes per chunk before flushing |
| `EVENT_STORE_FLUSH_INTERVAL` | `30` | Seconds between periodic flushes |
| `EVENT_STORE_S3_CHUNK_CACHE_TTL` | `300` | Seconds to cache S3 chunk indexes in each process |
| `EVENT_CACHE_BACKEND` | `filesystem` | Immutable S3 chunk cache: `filesystem` or `disabled` |
| `EVENT_CACHE_PATH` | `~/.intaris/event-cache` | Local cache path; used only by the S3 event backend |
| `EVENT_CACHE_MAX_BYTES` | `10737418240` | Maximum local event cache size |
| `EVENT_CACHE_TTL_SECONDS` | `604800` | Remove chunks not accessed within this interval |
| `EVENT_CACHE_TOUCH_INTERVAL_SECONDS` | `3600` | Minimum interval between persistent access-time updates |
| `EVENT_CACHE_SWEEP_INTERVAL_SECONDS` | `60` | Minimum interval between cache eviction sweeps |

Spooling is enabled by default and is **not an offline-write queue**. Startup
requires a reachable backend; known backend failures stop new writes. Because
uploads are batched and probes are periodic, an outage may be detected after
earlier requests have already been acknowledged locally. Those files remain
available for recovery. Probes verify connectivity/access, not a guarantee that
the next write will succeed. The existing flush settings control S3 chunk size
and archival latency, independently of local durability.

`EVENT_STORE_SPOOL_FSYNC=true` is the default and syncs spool files and directory
updates before their commit boundaries. Set it to `false` to skip those barriers
while retaining flushed writes, checksums, and atomic replacement. This retains
process/container-restart protection while the host kernel and volume survive,
but acknowledged data can be lost or inconsistently persisted after a node/kernel
crash, reboot, or power loss. It does not change database or archive durability.

Compression applies at archival, not to the pending batch files or the HTTP
protocol. Readers automatically accept mixed compressed and legacy chunks.
Unfinished exports keep their frozen format and exact bytes across configuration
changes. `EVENT_STORE_FLUSH_BYTES` still targets approximate **uncompressed**
bytes; compression does not enlarge the event-count or time thresholds.

Upgrade all readers before enabling compressed writers. For a staged rollout,
set `EVENT_STORE_COMPRESSION=none` until all readers support the new format.
Setting it back to `none` does not convert existing compressed objects; older
versions cannot read them. Do not downgrade readers while compressed chunks
remain in the archive.

Use a dedicated disk-backed volume, not the immutable cache directory or tmpfs.
See [Local event spool](event-spool.md) for crash guarantees, deployment mounts,
capacity, drain requirements, and failure tests.

### S3 Backend

The S3 backend caches immutable chunk contents on local storage by default.
The filesystem event backend does not use this cache because its event files
are already local and authoritative.

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
