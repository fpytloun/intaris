# OpenClaw Integration

A plugin for [OpenClaw](https://github.com/fpytloun/openclaw/tree/v2026.3.13) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

> **Note:** The Intaris extension is not yet merged upstream. It is available in our fork at [`extensions/intaris/`](https://github.com/fpytloun/openclaw/tree/v2026.3.13/extensions/intaris) for version `v2026.3.13`.

## Features

The intaris extension hooks into OpenClaw's plugin system, intercepting every tool call via `before_tool_call` and evaluating it through Intaris's REST API. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management (including hierarchical sub-agent sessions)
- Behavioral analysis: periodic checkpoints, session completion signals, agent summaries
- Escalation polling with exponential backoff
- Intention tracking with reasoning context forwarding
- MCP tool proxy (upstream MCP tools registered as native OpenClaw agent tools)
- Session recording with buffered batch sends

## Setup

### 1. Configuration

The plugin can be configured via OpenClaw's settings UI (the manifest provides a config schema with UI hints) or via environment variables. Plugin config takes priority over environment variables.

| Plugin Config | Env Var | Default | Description |
|---|---|---|---|
| `url` | `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `apiKey` | `INTARIS_API_KEY` | (required) | API key for authentication |
| `userId` | `INTARIS_USER_ID` | (empty) | User ID. Optional if API key maps to a user. |
| `failOpen` | `INTARIS_FAIL_OPEN` | `false` | Allow tool calls when Intaris is unreachable. 4xx errors always block regardless. |
| `allowPaths` | `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent dirs for `allow_paths` policy. Supports `~` expansion. |
| `escalationTimeout` | `INTARIS_ESCALATION_TIMEOUT` | `0` (no timeout) | Max seconds to wait for escalation approval. |
| `checkpointInterval` | `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between periodic checkpoints. `0` = disabled. |
| `recording` | `INTARIS_SESSION_RECORDING` | `false` | Enable session recording. |
| `recordToolOutput` | `INTARIS_RECORD_TOOL_OUTPUT` | follows `recording` | Record full tool output in events. |
| `recordingFlushSize` | `INTARIS_RECORDING_FLUSH_SIZE` | `50` | Events per recording batch flush. |
| `recordingFlushMs` | `INTARIS_RECORDING_FLUSH_MS` | `10000` | Recording flush interval in milliseconds. |
| `mcpTools` | `INTARIS_MCP_TOOLS` | `true` | Enable MCP tool proxy (registers upstream MCP tools as native agent tools). |
| `mcpToolsCacheTtlMs` | `INTARIS_MCP_TOOLS_CACHE_TTL_MS` | `900000` (15 min) | MCP tool list cache TTL. |

**Note:** `agentId` is not configurable -- it is sourced from OpenClaw's hook context (`ctx.agentId`).

### 2. Recommended Configuration

Add the Intaris extension to your OpenClaw config. The `tools.alsoAllow` entry ensures Intaris's tool calls are permitted by OpenClaw's built-in permission system:

```jsonc
{
  "tools": {
    "alsoAllow": ["intaris"]
  },
  "plugins": {
    "load": {
      "paths": [
        "/path/to/openclaw/extensions/intaris"
      ]
    },
    "entries": {
      "intaris": {
        "enabled": true,
        "config": {
          "url": "http://localhost:8060",
          "apiKey": "int-your-api-key",
          "userId": "user@example.com",
          "failOpen": false,
          "allowPaths": "~/",
          "escalationTimeout": 0,
          "checkpointInterval": 25,
          "recording": true,
          "recordingFlushSize": 50,
          "recordingFlushMs": 10000
        }
      }
    }
  }
}
```

### 3. Installation

The extension lives in the `extensions/intaris/` directory of the OpenClaw repository. Until this is merged into Openclaw upstream, you have to install Openclaw from our fork or port extensions/intaris directory into your installation.

### 4. Verify

After starting OpenClaw, open the Intaris management UI at `http://localhost:8060/ui`. You should see a new session appear in the Sessions tab when OpenClaw starts and agent receives first message.

## How It Works

### Plugin Flow

The plugin registers 10 hooks plus an optional tool factory via OpenClaw's `api.on()` system:

1. **`session_start`**: Creates an Intaris session via `POST /api/v1/intention` with session ID format `oc-{uuid}`. Handles 409 conflict (session already exists) by reusing the session.
2. **`before_agent_start`**: Forwards the user prompt as reasoning context via `POST /api/v1/reasoning`. Includes the last assistant message as context to help interpret short user replies (e.g., "ok, do it").
3. **`before_tool_call`**: Core guardrail -- evaluates every tool call via `POST /api/v1/evaluate`:
   - **approve**: tool executes normally
   - **deny**: blocks execution with `{ block: true, blockReason: "[intaris] DENIED: ..." }`
   - **escalate**: enters polling loop with exponential backoff (2s, 4s, 8s, 16s, 30s cap), waiting for human approval in the Intaris UI
   - **session suspended**: polls session status until reactivated or terminated
   - **session terminated**: immediate block
4. **`after_tool_call`**: Records tool results when session recording is enabled.
5. **`llm_output`**: Captures last assistant text for intention context on the next user turn.
6. **`subagent_spawning`**: Links child Intaris session to parent via `parent_session_id`. Enriches session details with sub-agent metadata (label, mode, depth).
7. **`subagent_ended`**: Completes child Intaris session when sub-agent ends.
8. **`agent_end`**: Transitions session to `idle` status.
9. **`before_reset`**: Closes Intaris session when user sends `/new` or `/reset`.
10. **`session_end`**: Signals session completion (`PATCH /session/{id}/status` + `POST /session/{id}/agent-summary`).
11. **`gateway_stop`**: Cleanup -- flushes recording buffers, clears timers.

### MCP Tool Factory

When `mcpTools` is enabled (default), the plugin registers a tool factory that:

1. Eagerly fetches the MCP tool list from Intaris at plugin init
2. Returns cached MCP tools as native OpenClaw `AgentTool` objects on each agent run
3. Refreshes the cache in the background when stale (TTL-based)
4. Proxies tool execution through `POST /api/v1/mcp/call`
5. Safety evaluation happens server-side -- the `before_tool_call` hook skips evaluation for recognized MCP tools to avoid double evaluation

## Behavioral Analysis

The plugin supports Intaris's behavioral analysis pipeline:

- **Intention tracking**: User messages are forwarded as reasoning context. The `intention_pending` flag coordinates with the IntentionBarrier so the server waits for reasoning before evaluating.
- **Periodic checkpoints**: Every `checkpointInterval` evaluate calls, sends a checkpoint with call counts, decision breakdown, and recent tool names.
- **Session completion**: On session end, sends completion status and agent summary with session statistics.
- **Hierarchical sessions**: Sub-agent sessions are created with `parent_session_id` and depth tracking for chain analysis.

## Session Recording

When `recording` is enabled, the plugin buffers events in-memory per session and flushes them in batches:

- **Buffer size**: `recordingFlushSize` events (default: 50) triggers a flush
- **Flush interval**: `recordingFlushMs` milliseconds (default: 10s) periodic flush
- **Events recorded**: `message` (user/assistant), `tool_call`, `tool_result`
- **User message cleaning**: Inbound metadata blocks (sender info, conversation context) are stripped before recording
- **Sent via**: `POST /api/v1/session/{id}/events` with header `X-Intaris-Source: openclaw`
- **Non-blocking**: Recording failures never block tool execution

## Tool Name Conventions

When using the **plugin**, tool names are passed as-is from OpenClaw:

- Built-in tools: OpenClaw's native tool names
- MCP tools (via tool factory): `server_tool` format (e.g., `mnemory_add_memory`)

When configuring session policies, use these names:

```json
{
  "policy": {
    "allow_tools": ["read_file", "list_directory"],
    "deny_tools": ["execute_command"]
  }
}
```

## Troubleshooting

- **"Evaluation failed" with no server logs**: Missing user identity. If using `INTARIS_API_KEY` (single shared key), you must also set `INTARIS_USER_ID`. Alternatively, use `INTARIS_API_KEYS` on the server with a key mapped to your user_id.
- **Tool calls blocked unexpectedly**: Check `INTARIS_URL` and `INTARIS_API_KEY`. Verify the session appears in the Intaris UI.
- **"Cannot create session" errors**: Verify Intaris is running and reachable.
- **All tool calls allowed**: If `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable. Check connectivity.
- **Double evaluation**: Both plugin and MCP proxy configured. Use only one approach. The plugin's tool factory already skips `before_tool_call` evaluation for MCP tools.
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not `0`.
- **Session not completing**: If OpenClaw exits abnormally, the `session_end` hook may not fire. The server's background sweep transitions idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.
- **MCP tools not appearing**: Check that `mcpTools` is enabled (default: `true`) and upstream MCP servers are configured in Intaris.
