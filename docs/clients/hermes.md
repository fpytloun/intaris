# Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (by Nous Research) has a plugin system that enables tool call safety evaluation via lifecycle hooks and tool registry wrapping. The Intaris plugin intercepts every tool call and evaluates it through Intaris's safety pipeline before allowing execution.

## Install

```bash
pip install hermes-intaris
```

The plugin is auto-discovered by Hermes via the `hermes_agent.plugins` entry point. No additional configuration in Hermes is needed -- just set the environment variables.

Alternatively, for directory-based installation:

```bash
git clone https://github.com/fpytloun/intaris.git
cp -r intaris/integrations/hermes/hermes_intaris ~/.hermes/plugins/intaris
cp intaris/integrations/hermes/hermes_intaris/plugin.yaml ~/.hermes/plugins/intaris/
```

## Configure

Set environment variables before starting Hermes:

```bash
export INTARIS_API_KEY=your-api-key
export INTARIS_URL=http://localhost:8060
```

### Configuration Options

All configuration is via environment variables.

| Env Var | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (required) | API key for authentication |
| `INTARIS_USER_ID` | (empty) | User ID. Optional if API key maps to a user. |
| `INTARIS_FAIL_OPEN` | `false` | Allow tool calls when Intaris is unreachable. 4xx errors always block regardless. |
| `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent dirs for `allow_paths` policy. Supports `~` expansion. |
| `INTARIS_ESCALATION_TIMEOUT` | `0` (no timeout) | Max seconds to wait for escalation approval. |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between periodic checkpoints. `0` = disabled. |
| `INTARIS_SESSION_RECORDING` | `false` | Enable session recording. |
| `INTARIS_MCP_TOOLS` | `true` | Enable MCP tool proxy (registers upstream MCP tools as native Hermes tools). |
| `INTARIS_MCP_TOOLS_CACHE_TTL_S` | `900` (15 min) | MCP tool list cache TTL in seconds. |
| `INTARIS_RECORDING_FLUSH_SIZE` | `50` | Events per recording batch flush. |
| `INTARIS_RECORDING_FLUSH_INTERVAL` | `10` | Recording flush interval in seconds. |

**Note:** `agentId` is not configurable via env var -- Hermes does not expose an agent identifier to plugins. Sessions appear with the default agent ID.

## Verify

After starting Hermes, open the Intaris management UI at `http://localhost:8060/ui`. You should see a new session appear in the Sessions tab when Hermes starts a conversation.

## How It Works

### Tool Wrapping

Unlike other integrations that use hook-based blocking, the Hermes plugin uses **tool registry wrapping**. During initialization, it:

1. Imports `tools.registry` from Hermes
2. Captures every existing tool's original handler
3. Re-registers each tool with a guarded wrapper that calls `POST /api/v1/evaluate` first
4. If approved, calls the original handler and returns its result
5. If denied, returns `{"error": "BLOCKED by Intaris: ..."}` to the LLM
6. If escalated, polls for user decision with exponential backoff

This works because Hermes plugins load **after** built-in tools register, and the tool registry allows handler replacement.

### Plugin Flow

The plugin registers 6 lifecycle hooks plus the tool wrapping:

1. **`on_session_start`**: Creates an Intaris session via `POST /api/v1/intention` with session ID format `hm-{uuid}`.
2. **`pre_llm_call`**: Forwards the user message as reasoning context via `POST /api/v1/reasoning`. When session recording is enabled, the user message event is recorded first and `/reasoning` is called with `from_events=true`. Otherwise, includes the last assistant message as context.
3. **Tool wrapper**: Core guardrail -- evaluates every tool call via `POST /api/v1/evaluate`:
   - **approve**: calls original handler, returns result
   - **deny**: returns error JSON to the LLM (blocks execution)
   - **escalate**: enters polling only when `/evaluate` still returns unresolved escalation (2s, 4s, 8s, 16s, 30s cap)
   - **session suspended**: polls session status until reactivated or terminated
   - **session terminated**: immediate block
4. **`pre_tool_call`**: Records tool call events when session recording is enabled.
5. **`post_tool_call`**: Records tool results when session recording is enabled.
6. **`post_llm_call`**: Captures last assistant text for reasoning context on the next user turn.
7. **`on_session_end`**: Signals session completion (`PATCH /session/{id}/status` + `POST /session/{id}/agent-summary`).

### MCP Tool Proxy

When `INTARIS_MCP_TOOLS=true` (default), the plugin:

1. Fetches the MCP tool list from Intaris at plugin init
2. Registers each MCP tool as a native Hermes tool via `ctx.register_tool()`
3. Refreshes the cache when stale (TTL-based)
4. Proxies tool execution through `POST /api/v1/mcp/call`
5. Safety evaluation happens server-side -- MCP tools are not wrapped by the tool registry guard to avoid double evaluation

## Behavioral Analysis

The plugin supports Intaris's behavioral analysis pipeline:

- **Intention tracking**: User messages are forwarded as reasoning context. Intaris now tracks recent user-message arrival server-side, so evaluation waits for the IntentionBarrier without requiring client-managed state. The legacy `intention_pending` flag remains as backward-compatible redundancy.
- **Periodic checkpoints**: Every `INTARIS_CHECKPOINT_INTERVAL` evaluate calls, sends a checkpoint with call counts, decision breakdown, and recent tool names.
- **Session completion**: On session end, sends completion status and agent summary with session statistics.

## Session Recording

When `INTARIS_SESSION_RECORDING=true`, the plugin buffers events in-memory per session and flushes them in batches:

- **Buffer size**: `INTARIS_RECORDING_FLUSH_SIZE` events (default: 50) triggers a flush
- **Flush interval**: `INTARIS_RECORDING_FLUSH_INTERVAL` seconds (default: 10s) periodic flush
- **Events recorded**: `message` (user/assistant), `tool_call`, `tool_result`
- **Sent via**: `POST /api/v1/session/{id}/events` with header `X-Intaris-Source: hermes`
- **Non-blocking**: Recording failures never block tool execution

## Tool Name Conventions

When using the **plugin**, tool names are Hermes's native tool names:

- Built-in tools: `terminal`, `read_file`, `write_file`, `search_files`, `patch_file`, `web_search`, etc.
- MCP tools (via plugin): `server_tool` format (e.g., `mnemory_add_memory`)

When configuring session policies, use these names:

```json
{
  "policy": {
    "allow_tools": ["read_file", "search_files"],
    "deny_tools": ["terminal"]
  }
}
```

## Troubleshooting

- **"Evaluation failed" with no server logs**: Missing user identity. If using `INTARIS_API_KEY` (single shared key), you must also set `INTARIS_USER_ID`. Alternatively, use `INTARIS_API_KEYS` on the server with a key mapped to your user_id.
- **Tool calls blocked unexpectedly**: Check `INTARIS_URL` and `INTARIS_API_KEY`. Verify the session appears in the Intaris UI.
- **"Cannot create session" errors**: Verify Intaris is running and reachable.
- **All tool calls allowed**: If `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable. Check connectivity.
- **Plugin not loading**: Verify `INTARIS_API_KEY` is set (the `plugin.yaml` declares it in `requires_env`). Check `hermes plugins list`.
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not `0`.
- **Session not completing**: If Hermes exits abnormally, the `on_session_end` hook may not fire. The server's background sweep transitions idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.
- **MCP tools not appearing**: Check that `INTARIS_MCP_TOOLS=true` and upstream MCP servers are configured in Intaris.
