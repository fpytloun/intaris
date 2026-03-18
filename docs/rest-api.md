# REST API

All endpoints are mounted under `/api/v1`. Authentication is via `Authorization: Bearer <key>` or `X-API-Key: <key>` header.

OpenAPI documentation is available at `/api/v1/docs` when the server is running.

## Evaluation

### POST /evaluate

Evaluate a tool call for safety and alignment.

**Request:**

```json
{
  "session_id": "oc-ses_abc123",
  "agent_id": "opencode",
  "tool": "bash",
  "args": {"command": "git status"},
  "context": "Checking repository state"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | Session identifier |
| `agent_id` | string | no | Agent identifier (metadata) |
| `tool` | string | yes | Tool name |
| `args` | object | yes | Tool arguments |
| `context` | string | no | Additional context for the evaluator |

**Response:**

```json
{
  "call_id": "uuid-here",
  "decision": "approve",
  "reasoning": "Read-only tool call: bash (git status)",
  "risk": "low",
  "path": "fast",
  "latency_ms": 0,
  "session_status": "active",
  "status_reason": null
}
```

| Field | Type | Description |
|---|---|---|
| `call_id` | string | Unique identifier for this evaluation |
| `decision` | string | `approve`, `deny`, or `escalate` |
| `reasoning` | string | Explanation of the decision |
| `risk` | string | `low`, `medium`, `high`, or `critical` |
| `path` | string | Evaluation path: `fast`, `critical`, `llm`, or `alignment` |
| `latency_ms` | integer | Evaluation time in milliseconds |
| `session_status` | string | Current session status (if changed) |
| `status_reason` | string | Reason for status change (if any) |

**Status codes:** 200 (evaluated), 429 (rate limited), 400 (bad request), 500 (evaluation error)

## Sessions

### POST /intention

Create a new session or update an existing session's intention.

**Request:**

```json
{
  "session_id": "oc-ses_abc123",
  "intention": "Implement user authentication feature",
  "details": {
    "source": "opencode",
    "working_directory": "/home/user/project",
    "agent_type": "plan"
  },
  "policy": {
    "allow_tools": ["read", "glob", "grep"],
    "deny_tools": [],
    "allow_commands": [],
    "deny_commands": ["rm -rf"],
    "allow_paths": ["/home/user/other-project/*"],
    "deny_paths": ["/etc/*", "/root/*"]
  },
  "parent_session_id": "oc-ses_parent123"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | Session identifier |
| `intention` | string | yes | What the session is trying to accomplish (max 500 chars) |
| `details` | object | no | Session metadata (source, working_directory, agent_type, etc.) |
| `policy` | object | no | Session-specific allow/deny rules (fnmatch patterns) |
| `parent_session_id` | string | no | Parent session ID for hierarchical sessions |

**Response:** Session details with `session_id`, `intention`, `status`, timestamps, and counters.

### GET /sessions

List sessions with pagination and filtering.

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `page` | `1` | Page number |
| `per_page` | `20` | Items per page (max 100) |
| `status` | (all) | Filter by status: `active`, `idle`, `completed`, `suspended`, `terminated` |
| `agent_id` | (all) | Filter by agent ID |

**Response:**

```json
{
  "items": [...],
  "total": 42,
  "page": 1,
  "pages": 3
}
```

### GET /session/{session_id}

Get full session details including counters and metadata.

**Response:**

```json
{
  "session_id": "oc-ses_abc123",
  "user_id": "alice",
  "agent_id": "opencode",
  "intention": "Implement user authentication feature",
  "intention_source": "user",
  "status": "active",
  "details": {...},
  "policy": {...},
  "parent_session_id": null,
  "total_calls": 42,
  "approved_calls": 38,
  "denied_calls": 3,
  "escalated_calls": 1,
  "summary_count": 0,
  "created_at": "2026-03-12T10:00:00Z",
  "last_activity_at": "2026-03-12T10:30:00Z"
}
```

### PATCH /session/{session_id}

Update session intention or details.

**Request:**

```json
{
  "intention": "Updated intention text",
  "details": {"working_directory": "/new/path"}
}
```

### PATCH /session/{session_id}/status

Update session status.

**Request:**

```json
{
  "status": "completed"
}
```

Valid transitions: `active` -> `idle`/`completed`/`suspended`/`terminated`, `idle` -> `active`/`completed`/`suspended`/`terminated`, `suspended` -> `active`/`terminated`.

## Audit

### GET /audit

Query audit records with filtering and pagination.

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `session_id` | (all) | Filter by session ID |
| `decision` | (all) | Filter by decision: `approve`, `deny`, `escalate` |
| `risk` | (all) | Filter by risk level |
| `tool` | (all) | Filter by tool name |
| `path` | (all) | Filter by evaluation path |
| `record_type` | (all) | Filter by record type: `tool_call`, `reasoning`, `checkpoint`, `summary` |
| `resolved` | (all) | Filter escalations: `true` (resolved), `false` (pending) |
| `page` | `1` | Page number |
| `per_page` | `50` | Items per page (max 200) |

**Response:**

```json
{
  "items": [
    {
      "call_id": "uuid-here",
      "session_id": "oc-ses_abc123",
      "user_id": "alice",
      "agent_id": "opencode",
      "tool": "bash",
      "args_redacted": {"command": "git status"},
      "classification": "read",
      "decision": "approve",
      "risk": "low",
      "reasoning": "Read-only tool call: bash (git status)",
      "evaluation_path": "fast",
      "latency_ms": 0,
      "record_type": "tool_call",
      "intention": "Implement user authentication feature",
      "timestamp": "2026-03-12T10:00:00Z"
    }
  ],
  "total": 100,
  "page": 1,
  "pages": 2
}
```

### GET /audit/{call_id}

Get a single audit record by call ID.

### POST /decision

Resolve an escalated tool call (approve or deny).

**Request:**

```json
{
  "call_id": "uuid-of-escalated-call",
  "decision": "approve",
  "note": "Reviewed and approved — safe operation"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `call_id` | string | yes | Call ID of the escalated evaluation |
| `decision` | string | yes | `approve` or `deny` |
| `note` | string | no | Optional note explaining the decision |

## Behavioral Analysis

### POST /reasoning

Submit agent reasoning text for behavioral analysis.

**Request:**

```json
{
  "session_id": "oc-ses_abc123",
  "content": "User message: Please implement the login form"
}
```

When content starts with `"User message:"`, triggers the IntentionBarrier to regenerate the session intention.

### POST /checkpoint

Submit a periodic agent state checkpoint.

**Request:**

```json
{
  "session_id": "oc-ses_abc123",
  "content": "Checkpoint #3: 42 calls (38 approved, 3 denied, 1 escalated). Recent tools: bash, edit, read."
}
```

### POST /session/{session_id}/agent-summary

Submit an agent-reported session summary (stored separately from Intaris analysis).

**Request:**

```json
{
  "summary": "Implemented user authentication with JWT tokens. Modified 5 files, ran tests successfully."
}
```

### POST /session/{session_id}/summary/trigger

Manually trigger an Intaris session summary generation.

### GET /session/{session_id}/summary

Retrieve both Intaris-generated and agent-reported summaries for a session. Intaris summaries are ordered with compacted summaries first, then by creation time descending.

Each Intaris summary includes a `summary_type` field:
- `"window"` -- covers a specific time range within the session
- `"compacted"` -- synthesizes all windows into a single session-level assessment

### POST /analysis/trigger

Manually trigger a cross-session behavioral analysis. L3 analysis operates only on root sessions (`parent_session_id IS NULL`) -- child session data is already embedded in parent compacted summaries. Prefers compacted summaries when available.

### GET /analysis

List behavioral analyses with pagination.

### GET /profile

Get the behavioral risk profile for the authenticated user. Requires a user-bound API key (prevents agents from querying their own risk profile).

**Response:**

```json
{
  "user_id": "alice",
  "risk_level": "low",
  "active_alerts": [],
  "context_summary": "Normal usage patterns across 42 sessions.",
  "profile_version": 3
}
```

## Session Events (Recording)

### POST /session/{session_id}/events

Append events to the session recording.

**Request:**

```json
{
  "events": [
    {"type": "tool_call", "data": {"tool": "bash", "args": {"command": "ls"}}},
    {"type": "tool_result", "data": {"output": "file1.py\nfile2.py"}}
  ]
}
```

Or a single event:

```json
{
  "type": "message",
  "data": {"role": "user", "content": "Please fix the bug"}
}
```

**Response:**

```json
{
  "ok": true,
  "count": 2,
  "first_seq": 1,
  "last_seq": 2
}
```

### GET /session/{session_id}/events

Read events with pagination and filtering.

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `after_seq` | `0` | Return events after this sequence number |
| `limit` | `100` | Max events to return |
| `type` | (all) | Filter by event type |
| `after_ts` | (none) | Return events after this ISO 8601 timestamp |
| `before_ts` | (none) | Return events before this ISO 8601 timestamp |

**Response:**

```json
{
  "events": [
    {"seq": 1, "ts": "2026-03-12T10:00:00.123Z", "type": "message", "source": "user", "data": {...}}
  ],
  "last_seq": 42,
  "has_more": true
}
```

### POST /session/{session_id}/events/flush

Force flush buffered events to storage.

## MCP Servers

### GET /mcp/servers

List all configured MCP servers.

### GET /mcp/servers/{name}

Get a single server configuration.

### PUT /mcp/servers/{name}

Create or update an MCP server.

**Request:**

```json
{
  "transport": "streamable-http",
  "config": {
    "url": "https://example.com/mcp",
    "headers": {"Authorization": "Bearer token"}
  },
  "enabled": true
}
```

### DELETE /mcp/servers/{name}

Delete an MCP server.

### POST /mcp/servers/{name}/refresh

Force-refresh the tools cache from the upstream server.

### GET /mcp/servers/{name}/preferences/{tool_name}

Get the preference for a specific tool.

### PUT /mcp/servers/{name}/preferences/{tool_name}

Set a tool preference.

**Request:**

```json
{
  "preference": "auto-approve"
}
```

Valid preferences: `auto-approve`, `escalate`, `deny`.

### DELETE /mcp/servers/{name}/preferences/{tool_name}

Remove a tool preference (revert to default evaluation).

### GET /mcp/tools

List all available MCP tools aggregated from configured upstream servers. Tools are filtered by agent pattern and tool preferences (denied tools are excluded).

Used by the OpenClaw plugin to discover and register MCP tools as agent tools.

**Response:**

```json
{
  "tools": [
    {
      "server": "github",
      "name": "create_issue",
      "description": "Creates a GitHub issue",
      "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}
    }
  ]
}
```

Returns `{"tools": []}` if the MCP proxy is not configured.

### POST /mcp/call

Call an MCP tool via the REST proxy. The call goes through the full safety evaluation pipeline (tool preferences + LLM evaluation + audit logging) before being forwarded to the upstream MCP server.

Used by the OpenClaw plugin to proxy MCP tool calls through Intaris with safety evaluation.

**Request:**

```json
{
  "session_id": "oc-abc123",
  "server": "github",
  "tool": "create_issue",
  "arguments": {"title": "Bug report", "body": "..."}
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | Intaris session ID (from the OpenClaw plugin) |
| `server` | string | yes | MCP server name |
| `tool` | string | yes | Tool name on the MCP server |
| `arguments` | object | no | Tool arguments (default: `{}`) |

**Response:**

```json
{
  "content": [{"type": "text", "text": "Issue created: #42"}],
  "isError": false,
  "decision": "approve",
  "call_id": "call_abc123",
  "reasoning": null,
  "latency_ms": 350
}
```

| Field | Type | Description |
|---|---|---|
| `content` | array | Tool result content (text blocks) |
| `isError` | boolean | Whether the call resulted in an error |
| `decision` | string | Safety evaluation decision: `approve`, `deny`, or `escalate` |
| `call_id` | string | Audit call ID |
| `reasoning` | string | Evaluation reasoning (for deny/escalate) |
| `latency_ms` | number | Total latency including evaluation and upstream call |

## Notifications

### GET /notifications/channels

List notification channels for the authenticated user.

### GET /notifications/channels/{name}

Get a single notification channel.

### PUT /notifications/channels/{name}

Create or update a notification channel.

**Request:**

```json
{
  "provider": "pushover",
  "config": {"user_key": "...", "api_token": "..."},
  "enabled": true,
  "events": ["escalation", "resolution", "session_suspended"]
}
```

Supported providers: `webhook`, `pushover`, `slack`.

### DELETE /notifications/channels/{name}

Delete a notification channel.

### POST /notifications/channels/{name}/test

Send a test notification through the channel.

## Info

### GET /whoami

Returns the authenticated user's identity and capabilities.

**Response:**

```json
{
  "user_id": "alice",
  "agent_id": "opencode",
  "user_bound": true,
  "can_switch_user": false
}
```

### GET /stats

Aggregated dashboard metrics.

**Response includes:** session counts by status, evaluation totals by decision/risk/path/classification, active/idle session counts, MCP proxy stats.

### GET /config

Non-sensitive server configuration (LLM base URL masked, no secrets).

## WebSocket Streaming

### WS /stream

Real-time event streaming via WebSocket.

**Protocol:**

1. Connect to `ws://host/api/v1/stream`
2. Send auth message:
   ```json
   {"type": "auth", "token": "Bearer your-key", "user_id": "alice", "session_id": "oc-ses_abc123"}
   ```
3. Receive events as JSON messages
4. Server sends `{"type": "ping"}` every 30s as keepalive

**Event types:** `evaluation`, `decision`, `session_event`, `session_status`

**Limits:** 10 concurrent WebSocket connections per user. Auth failure closes with code 4001.

## Health

### GET /health

Health check endpoint (no authentication required).

**Response:**

```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

## Error Responses

All error responses follow this format:

```json
{
  "error": "Short error description",
  "detail": "Detailed explanation (optional)"
}
```

**Status codes:**

| Code | Description |
|---|---|
| 400 | Bad request (validation error) |
| 401 | Unauthorized (missing or invalid API key) |
| 403 | Forbidden (user not authorized for this resource) |
| 404 | Not found |
| 429 | Rate limit exceeded |
| 500 | Internal server error |
