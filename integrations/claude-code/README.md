# Claude Code Integration -- Guardrails

A hooks-based integration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

## Integration Approaches

Intaris offers two integration approaches for Claude Code. Choose one -- do **not** use both simultaneously, or tool calls will be evaluated twice.

### Approach A: Hooks (Recommended)

Shell script hooks configured in Claude Code's `settings.json` that intercept `PreToolUse` events and call Intaris's REST API. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management
- Debug logging via stderr
- Behavioral analysis: periodic checkpoints, session completion signals, agent summaries

### Approach B: MCP Proxy

Configure Intaris as a remote MCP server in `claude_code_config.json`. Claude Code connects to Intaris's `/mcp` endpoint, which transparently proxies all tool calls through the safety pipeline. This gives you:

- Zero hook code -- just configuration
- Full MCP proxy features (tool preferences, escalation retry, tool namespacing)
- Works with any MCP-compatible client

The trade-off is less control over the UX (no custom error messages, no fail-open option).

## Setup -- Hooks (Approach A)

### 1. Environment Variables

The hooks make direct HTTP calls to the Intaris REST API. Set these in your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-api-key
export INTARIS_AGENT_ID=claude-code        # optional, defaults to "claude-code"
export INTARIS_USER_ID=your-username       # optional if API key maps to user
export INTARIS_FAIL_OPEN=false             # optional, defaults to false
export INTARIS_INTENTION=""                # optional, auto-generated from cwd
export INTARIS_CHECKPOINT_INTERVAL=25      # optional, defaults to 25 (0=disabled)
export INTARIS_DEBUG=false                 # optional, enable stderr logging
```

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication. **Required** if Intaris has `INTARIS_API_KEYS` set. |
| `INTARIS_AGENT_ID` | `claude-code` | Agent ID sent to Intaris |
| `INTARIS_USER_ID` | (empty) | User ID (optional if API key maps to a user) |
| `INTARIS_FAIL_OPEN` | `false` | If `true`, tool calls proceed when Intaris is unreachable. Default is `false` (fail-closed) -- tool calls are blocked when Intaris is down. |
| `INTARIS_INTENTION` | (auto) | Session intention override. Default: `"Claude Code coding session in <cwd>"` |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Number of evaluate calls between periodic checkpoints. Set to `0` to disable checkpoints. Each checkpoint consumes one rate limit slot. |
| `INTARIS_DEBUG` | `false` | Enable debug logging to stderr |

### 2. Install the Hooks

Copy the hooks configuration and scripts:

```bash
# Copy scripts
mkdir -p ~/.claude/scripts
cp integrations/claude-code/scripts/session.sh ~/.claude/scripts/intaris-session.sh
cp integrations/claude-code/scripts/evaluate.sh ~/.claude/scripts/intaris-evaluate.sh
cp integrations/claude-code/scripts/stop.sh ~/.claude/scripts/intaris-stop.sh
chmod +x ~/.claude/scripts/intaris-*.sh

# Copy hooks config
cp integrations/claude-code/hooks.json ~/.claude/settings.json
# Or merge with existing settings.json if you have other hooks
```

If you already have a `~/.claude/settings.json`, merge the `hooks` section from `hooks.json` into it. The hooks section includes `SessionStart`, `PreToolUse`, and `Stop` hooks.

### 3. Verify

Enable debug logging and run Claude Code:

```bash
export INTARIS_DEBUG=true
claude
```

Look for `[intaris]` messages in stderr:

```
[intaris] Creating session: cc-<session-id>
[intaris] Session created: cc-<session-id>
[intaris] Evaluating: bash
[intaris] bash: approve (fast, 12ms, risk=)
[intaris] Sending checkpoint #1
[intaris] Signaling completion for session: cc-<session-id>
```

## Setup -- MCP Proxy (Approach B)

Add to `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "intaris": {
      "type": "streamable-http",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-api-key",
        "X-Agent-Id": "claude-code",
        "X-Intaris-Intention": "Claude Code coding session"
      }
    }
  }
}
```

Configure upstream MCP servers in Intaris (via the UI, REST API, or `MCP_CONFIG_FILE`). Claude Code will see all upstream tools namespaced as `server_name:tool_name`.

## How It Works

### Hooks Flow

1. **`SessionStart`** (on startup/resume): Creates an Intaris session via `POST /api/v1/intention` with the working directory as context. Stores session state as JSON in a temp file.
2. **`PreToolUse`** (before every tool call):
   - Loads session state from the JSON temp file (or creates one lazily)
   - Calls `POST /api/v1/evaluate` with the tool name and arguments
   - **approve**: outputs `{}` (allow)
   - **deny**: outputs `{"decision": "block", "reason": "..."}` (blocks execution)
   - **escalate**: outputs `{"decision": "block", "reason": "..."}` (blocks with approval instructions)
   - Updates session statistics (call count, decision breakdown, recent tools)
   - Sends periodic checkpoints via `POST /api/v1/checkpoint` (every N calls)
3. **`Stop`** (on session end):
   - Signals session completion: `PATCH /api/v1/session/{id}/status` to `"completed"`
   - Sends agent summary: `POST /api/v1/session/{id}/agent-summary` with session statistics
   - Both HTTP calls run in parallel to stay within the hook timeout
   - Cleans up the temp state file

### MCP Proxy Flow

1. Claude Code connects to Intaris at `/mcp` as a Streamable HTTP MCP server.
2. `tools/list` returns aggregated tools from all upstream servers.
3. `tools/call` evaluates each call through the safety pipeline before forwarding.

## Behavioral Analysis

The hooks support Intaris's behavioral analysis pipeline (L1 data collection):

- **Periodic checkpoints**: Every `INTARIS_CHECKPOINT_INTERVAL` evaluate calls, the hook sends a checkpoint with enriched content (call counts, decision breakdown, recent tool names). Checkpoints share the rate limit budget with evaluate calls.
- **Session completion**: The `Stop` hook signals session completion and sends an agent summary with session statistics (total calls, approve/deny/escalate breakdown, working directory).
- **Session state tracking**: State is persisted as JSON in `/tmp/intaris_state_*.json` files across hook invocations.

## Tool Name Conventions

When using the **hooks** approach, tool names are passed as-is from Claude Code to Intaris. Claude Code uses these tool names:

- Built-in tools: `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, `WebFetch`, `Task`
- MCP tools: `mcp__server__tool` format (e.g., `mcp__mnemory__add_memory`)

When configuring Intaris session policies (fnmatch patterns), use these names:

```json
{
  "policy": {
    "allow": ["Read", "Glob", "Grep"],
    "deny": ["Bash"]
  }
}
```

Note: Claude Code tool names are **capitalized** (e.g., `Bash` not `bash`). MCP tools use double underscores (e.g., `mcp__server__tool`).

When using the **MCP proxy** approach, tools are namespaced as `server_name:tool_name` (e.g., `mnemory:add_memory`).

## Session State Tracking

The hooks use JSON state files (`/tmp/intaris_state_*.json`) to track session state across hook invocations. Each file contains:

```json
{
  "session_id": "cc-<session-id>",
  "call_count": 42,
  "approved": 38,
  "denied": 3,
  "escalated": 1,
  "recent_tools": ["Bash", "Edit", "Read"],
  "cwd": "/path/to/project"
}
```

State files are created by `SessionStart` and cleaned up by `Stop`. They accumulate if Claude Code exits abnormally. Clean up manually:

```bash
rm /tmp/intaris_state_*
```

The evaluate hook supports backward compatibility with legacy state files (plain session ID format) during upgrades.

## Troubleshooting

- **Tool calls blocked unexpectedly**: Check that `INTARIS_URL` and `INTARIS_API_KEY` are set. Enable `INTARIS_DEBUG=true` and check stderr for evaluation decisions.
- **Scripts not executing**: Ensure they're executable (`chmod +x ~/.claude/scripts/intaris-*.sh`). Check Claude Code logs for hook errors.
- **"Cannot create session" errors**: Verify Intaris is running and reachable at the configured URL.
- **All tool calls allowed**: If using `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable. Check connectivity.
- **Slow first tool call**: If `SessionStart` didn't fire, the first `PreToolUse` creates a session (~1-2s) before evaluating. Total time should be under 10s (the hook timeout).
- **Double evaluation**: If you see each tool call evaluated twice, you may have both hooks and MCP proxy configured. Use only one approach.
- **`jq` not found**: The scripts require `jq` for JSON processing. Install it: `brew install jq` (macOS) or `apt install jq` (Linux). The `Stop` hook exits gracefully if `jq` is not available.
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not set to `0`. Check that the rate limit is not exhausted (checkpoints share the budget with evaluate calls).
- **Stop hook not firing**: If Claude Code crashes or is killed, the `Stop` hook may not fire. The server's background sweep handles this by transitioning idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.
