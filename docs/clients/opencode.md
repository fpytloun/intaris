# OpenCode Integration

A plugin for [OpenCode](https://opencode.ai) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

## Integration Approaches

Intaris offers two integration approaches for OpenCode. Choose one -- do **not** use both simultaneously, or tool calls will be evaluated twice.

### Approach A: Plugin (Recommended)

The `intaris.ts` plugin intercepts every tool call via `tool.execute.before` and evaluates it through Intaris's REST API. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management (including parent/child sessions)
- Behavioral analysis: periodic checkpoints, session completion signals, agent summaries
- Escalation polling with exponential backoff

### Approach B: MCP Proxy

Configure Intaris as a remote MCP server. OpenCode connects to Intaris's `/mcp` endpoint, which transparently proxies all tool calls through the safety pipeline. This gives you:

- Zero plugin code -- just configuration
- Full MCP proxy features (tool preferences, escalation retry, tool namespacing)
- Works with any MCP-compatible client

The trade-off is less control over the UX (no custom error messages, no fail-open option).

## Setup -- Plugin (Approach A)

### 1. Environment Variables

Set these in your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-api-key
export INTARIS_USER_ID=your-username       # required for single-key mode
export INTARIS_AGENT_ID=opencode           # optional, defaults to "opencode"
export INTARIS_FAIL_OPEN=false             # optional, defaults to false
export INTARIS_INTENTION=""                # optional, auto-generated from cwd
export INTARIS_ALLOW_PATHS=~/src           # optional, allow reads from sibling projects
export INTARIS_CHECKPOINT_INTERVAL=25      # optional, defaults to 25 (0=disabled)
```

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication. **Required** if Intaris has `INTARIS_API_KEY` or `INTARIS_API_KEYS` set. |
| `INTARIS_AGENT_ID` | `opencode` | Agent ID sent to Intaris |
| `INTARIS_USER_ID` | (empty) | User ID. **Required** when using `INTARIS_API_KEY` (single-key mode). Optional if `INTARIS_API_KEYS` maps your key to a specific user. |
| `INTARIS_FAIL_OPEN` | `false` | If `true`, tool calls proceed when Intaris is unreachable. Default is `false` (fail-closed). |
| `INTARIS_INTENTION` | (auto) | Session intention override. Default: `"OpenCode coding session in <cwd>"` |
| `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent directories to allow reads from without LLM evaluation. Supports `~` expansion. E.g., `~/src` allows reads from all projects under `~/src/`. |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between periodic checkpoints. Set to `0` to disable. |

### 2. Install the Plugin

Copy `intaris.ts` to your OpenCode plugins directory:

```bash
# Global (recommended -- guardrails apply to all projects)
mkdir -p ~/.config/opencode/plugins
cp integrations/opencode/intaris.ts ~/.config/opencode/plugins/

# Or project-level
mkdir -p .opencode/plugins
cp intaris.ts .opencode/plugins/
```

Local plugins are loaded automatically -- no config entry needed.

### 3. Disable OpenCode's Built-in Approvals

OpenCode has its own permission system that prompts for approval on certain actions. When using the Intaris plugin, this creates double prompting. To let Intaris be the sole gatekeeper:

```jsonc
// opencode.jsonc
{
  // Let Intaris handle all tool approval decisions
  "permission": "allow"
}
```

Or, to only disable the external directory prompt:

```jsonc
{
  "permission": {
    "external_directory": "allow"
  }
}
```

See [OpenCode Permissions](https://opencode.ai/docs/permissions/) for details.

### 4. Verify

Run OpenCode with `--print-logs` and look for:

```
[intaris] Plugin initialized
[intaris] Session created: oc-<session-id>
```

## Setup -- MCP Proxy (Approach B)

Add to your `~/.config/opencode/opencode.json` (global) or `opencode.json` (project):

```json
{
  "mcp": {
    "intaris": {
      "type": "remote",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-api-key",
        "X-Agent-Id": "opencode",
        "X-Intaris-Intention": "OpenCode coding session"
      }
    }
  }
}
```

Configure upstream MCP servers in Intaris (via the UI, REST API, or `MCP_CONFIG_FILE`). OpenCode will see all upstream tools namespaced as `server_name:tool_name`.

See the [MCP Proxy Guide](../mcp-proxy.md) for full details.

## How It Works

### Plugin Flow

1. **`session.created`**: Creates an Intaris session via `POST /api/v1/intention` with the working directory as context. Child sessions (subagent tasks) are created with `parent_session_id` for session chain analysis.
2. **`tool.execute.before`**: Before every tool call:
   - Ensures an Intaris session exists (lazy creation for resumed sessions)
   - Calls `POST /api/v1/evaluate` with the tool name and arguments
   - **approve**: tool executes normally
   - **deny**: throws an error with reasoning (blocks execution)
   - **escalate**: enters polling loop with exponential backoff, waiting for human approval
   - Tracks per-decision statistics (approve/deny/escalate counts)
   - Sends periodic checkpoints via `POST /api/v1/checkpoint`
3. **`session.deleted`**: Signals session completion:
   - `PATCH /api/v1/session/{id}/status` to `"completed"`
   - `POST /api/v1/session/{id}/agent-summary` with session statistics

### MCP Proxy Flow

1. OpenCode connects to Intaris at `/mcp` as a remote MCP server
2. `tools/list` returns aggregated tools from all upstream servers
3. `tools/call` evaluates each call through the safety pipeline before forwarding

## Behavioral Analysis

The plugin supports Intaris's behavioral analysis pipeline:

- **Periodic checkpoints**: Every `INTARIS_CHECKPOINT_INTERVAL` evaluate calls, the plugin sends a checkpoint with call counts, decision breakdown, and recent tool names
- **Session completion**: On session deletion, sends completion status and agent summary with session statistics
- **Parent session tracking**: Child sessions are created with `parent_session_id` for chain analysis

**Limitation**: OpenCode does not fire a session-end event on normal exit. If the user closes OpenCode without deleting the session, the completion signal is not sent. The server's background sweep handles this by transitioning idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.

## Tool Name Conventions

When using the **plugin**, tool names are passed as-is from OpenCode:

- Built-in tools: `read`, `edit`, `write`, `bash`, `glob`, `grep`
- MCP tools: The MCP tool name directly (e.g., `add_memory`, `search_memories`)

When configuring session policies, use these names:

```json
{
  "policy": {
    "allow_tools": ["read", "glob", "grep"],
    "deny_tools": ["bash"]
  }
}
```

When using the **MCP proxy**, tools are namespaced as `server_name:tool_name`.

## Troubleshooting

- **"Evaluation failed" with no server logs**: Missing user identity. If using `INTARIS_API_KEY` (single shared key), you must also set `INTARIS_USER_ID`. Alternatively, use `INTARIS_API_KEYS` on the server with a key mapped to your user_id.
- **Tool calls blocked unexpectedly**: Check `INTARIS_URL` and `INTARIS_API_KEY`. Run OpenCode with `--print-logs` to see evaluation decisions.
- **"Cannot create session" errors**: Verify Intaris is running and reachable.
- **All tool calls allowed**: Ensure the plugin is loaded (check for "Plugin initialized" in logs). If `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable.
- **Slow first tool call**: The first call creates an Intaris session (~1-2s). Subsequent calls are faster.
- **Double evaluation**: Both plugin and MCP proxy configured. Use only one approach.
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not `0`. Check rate limit budget.
