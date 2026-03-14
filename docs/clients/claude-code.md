# Claude Code Integration

A hooks-based integration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

## Integration Approaches

Intaris offers two integration approaches for Claude Code. Choose one -- do **not** use both simultaneously, or tool calls will be evaluated twice.

### Approach A: Hooks (Recommended)

Shell script hooks configured in Claude Code's `settings.json` that intercept tool use events and call Intaris's REST API. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management
- Debug logging via stderr
- Behavioral analysis: periodic checkpoints, session completion signals, agent summaries
- Session recording (optional)

### Approach B: MCP Proxy

Configure Intaris as a remote MCP server in `claude_code_config.json`. Claude Code connects to Intaris's `/mcp` endpoint, which transparently proxies all tool calls through the safety pipeline. This gives you:

- Zero hook code -- just configuration
- Full MCP proxy features (tool preferences, escalation retry, tool namespacing)
- Works with any MCP-compatible client

The trade-off is less control over the UX (no custom error messages, no fail-open option).

## Setup -- Hooks (Approach A)

### 1. Environment Variables

Set these in your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-api-key
export INTARIS_AGENT_ID=claude-code        # optional, defaults to "claude-code"
export INTARIS_USER_ID=your-username       # optional if API key maps to user
export INTARIS_FAIL_OPEN=false             # optional, defaults to false
export INTARIS_INTENTION=""                # optional, auto-generated from cwd
export INTARIS_ALLOW_PATHS=~/src           # optional, allow reads from sibling projects
export INTARIS_CHECKPOINT_INTERVAL=25      # optional, defaults to 25 (0=disabled)
export INTARIS_SESSION_RECORDING=false     # optional, enable session recording
export INTARIS_DEBUG=false                 # optional, enable stderr logging
```

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication. **Required** if Intaris has auth configured. |
| `INTARIS_AGENT_ID` | `claude-code` | Agent ID sent to Intaris |
| `INTARIS_USER_ID` | (empty) | User ID (optional if API key maps to a user) |
| `INTARIS_FAIL_OPEN` | `false` | If `true`, tool calls proceed when Intaris is unreachable. |
| `INTARIS_INTENTION` | (auto) | Session intention override. Default: `"Claude Code coding session in <cwd>"` |
| `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent directories for cross-project reads. Supports `~` expansion. |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between periodic checkpoints. `0` = disabled. |
| `INTARIS_SESSION_RECORDING` | `false` | Enable session recording for playback and analysis. |
| `INTARIS_DEBUG` | `false` | Enable debug logging to stderr. |

### 2. Install the Hooks

Copy the hooks configuration and scripts:

```bash
# Copy scripts
mkdir -p ~/.claude/scripts
cp integrations/claude-code/scripts/session.sh ~/.claude/scripts/intaris-session.sh
cp integrations/claude-code/scripts/evaluate.sh ~/.claude/scripts/intaris-evaluate.sh
cp integrations/claude-code/scripts/record.sh ~/.claude/scripts/intaris-record.sh
cp integrations/claude-code/scripts/stop.sh ~/.claude/scripts/intaris-stop.sh
chmod +x ~/.claude/scripts/intaris-*.sh

# Copy hooks config
cp integrations/claude-code/hooks.json ~/.claude/settings.json
# Or merge with existing settings.json if you have other hooks
```

The hooks section includes `SessionStart`, `PreToolUse`, `PostToolUse`, and `Stop` hooks.

### 3. Prerequisites

The scripts require `jq` for JSON processing:

```bash
# macOS
brew install jq

# Linux
apt install jq
```

### 4. Verify

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

See the [MCP Proxy Guide](../mcp-proxy.md) for full details.

## How It Works

### Hooks Flow

1. **`SessionStart`** (on startup/resume): Creates an Intaris session via `POST /api/v1/intention` with the working directory as context. Stores session state as JSON in a temp file.
2. **`PreToolUse`** (before every tool call):
   - Loads session state from the JSON temp file (or creates one lazily)
   - Calls `POST /api/v1/evaluate` with the tool name and arguments
   - **approve**: outputs `{}` (allow)
   - **deny**: outputs `{"decision": "block", "reason": "..."}` (blocks execution)
   - **escalate**: outputs `{"decision": "block", "reason": "..."}` with approval instructions
   - Updates session statistics and sends periodic checkpoints
3. **`PostToolUse`** (after tool execution): Records tool results when session recording is enabled.
4. **`Stop`** (on session end):
   - Signals session completion: `PATCH /api/v1/session/{id}/status` to `"completed"`
   - Sends agent summary: `POST /api/v1/session/{id}/agent-summary` with session statistics
   - Cleans up the temp state file

### MCP Proxy Flow

1. Claude Code connects to Intaris at `/mcp` as a Streamable HTTP MCP server
2. `tools/list` returns aggregated tools from all upstream servers
3. `tools/call` evaluates each call through the safety pipeline before forwarding

## Session Recording

When `INTARIS_SESSION_RECORDING=true`, the hooks record tool calls and results to the Intaris event store for session playback and analysis.

### What's Recorded

- **`PreToolUse`**: `tool_call` events with tool name, arguments, and evaluation decision
- **`PostToolUse`**: `tool_result` events with tool output and error status
- **`Stop`**: Full Claude Code transcript (JSONL) as `transcript` events

### How It Works

Unlike the OpenCode plugin (which buffers events client-side), the Claude Code hooks send events directly on each invocation since bash scripts are stateless. Each hook call sends 1-2 events via `POST /session/{id}/events`. The server-side EventStore buffers and consolidates these into chunks.

Recording is completely non-blocking -- all recording API calls are fire-and-forget with 2s timeouts. Recording failures never block tool execution.

## Behavioral Analysis

The hooks support Intaris's behavioral analysis pipeline:

- **Periodic checkpoints**: Every `INTARIS_CHECKPOINT_INTERVAL` evaluate calls, sends a checkpoint with call counts, decision breakdown, and recent tool names
- **Session completion**: The `Stop` hook signals completion and sends an agent summary with session statistics
- **Session state tracking**: State persisted as JSON in `/tmp/intaris_state_*.json` files

## Tool Name Conventions

When using **hooks**, tool names are passed as-is from Claude Code:

- Built-in tools: `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, `WebFetch`, `Task` (capitalized)
- MCP tools: `mcp__server__tool` format (e.g., `mcp__mnemory__add_memory`)

When configuring session policies, use these names:

```json
{
  "policy": {
    "allow_tools": ["Read", "Glob", "Grep"],
    "deny_tools": ["Bash"]
  }
}
```

Note: Claude Code tool names are **capitalized**. MCP tools use **double underscores**.

When using the **MCP proxy**, tools are namespaced as `server_name:tool_name`.

## Session State

The hooks use JSON state files (`/tmp/intaris_state_*.json`) to track session state across invocations:

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

State files are created by `SessionStart` and cleaned up by `Stop`. They accumulate if Claude Code exits abnormally:

```bash
# Manual cleanup
rm /tmp/intaris_state_*
```

## Troubleshooting

- **Tool calls blocked unexpectedly**: Check `INTARIS_URL` and `INTARIS_API_KEY`. Enable `INTARIS_DEBUG=true` and check stderr.
- **Scripts not executing**: Ensure they're executable (`chmod +x ~/.claude/scripts/intaris-*.sh`). Check Claude Code logs for hook errors.
- **"Cannot create session" errors**: Verify Intaris is running and reachable.
- **All tool calls allowed**: If `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable. Check connectivity.
- **Slow first tool call**: If `SessionStart` didn't fire, the first `PreToolUse` creates a session (~1-2s). Total time should be under 10s.
- **Double evaluation**: Both hooks and MCP proxy configured. Use only one approach.
- **`jq` not found**: Install it: `brew install jq` (macOS) or `apt install jq` (Linux).
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not `0`. Check rate limit budget.
- **Stop hook not firing**: If Claude Code crashes, the `Stop` hook may not fire. The server's background sweep transitions idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.
