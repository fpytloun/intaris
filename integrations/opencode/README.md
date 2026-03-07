# OpenCode Integration -- Guardrails

A plugin for [OpenCode](https://opencode.ai) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

## Integration Approaches

Intaris offers two integration approaches for OpenCode. Choose one -- do **not** use both simultaneously, or tool calls will be evaluated twice.

### Approach A: Plugin (Recommended)

The `intaris.ts` plugin intercepts every tool call via `tool.execute.before` and evaluates it through Intaris's REST API. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management
- Structured logging via OpenCode's log system

### Approach B: MCP Proxy

Configure Intaris as a remote MCP server. OpenCode connects to Intaris's `/mcp` endpoint, which transparently proxies all tool calls through the safety pipeline. This gives you:

- Zero plugin code -- just configuration
- Full MCP proxy features (tool preferences, escalation retry, tool namespacing)
- Works with any MCP-compatible client

The trade-off is less control over the UX (no custom error messages, no fail-open option).

## Setup -- Plugin (Approach A)

### 1. Environment Variables

The plugin makes direct HTTP calls to the Intaris REST API. Set these in your shell profile:

```bash
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-api-key
export INTARIS_AGENT_ID=opencode           # optional, defaults to "opencode"
export INTARIS_USER_ID=your-username       # optional if API key maps to user
export INTARIS_FAIL_OPEN=false             # optional, defaults to false
export INTARIS_INTENTION=""                # optional, auto-generated from cwd
```

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication. **Required** if Intaris has `INTARIS_API_KEYS` set. |
| `INTARIS_AGENT_ID` | `opencode` | Agent ID sent to Intaris |
| `INTARIS_USER_ID` | (empty) | User ID (optional if API key maps to a user) |
| `INTARIS_FAIL_OPEN` | `false` | If `true`, tool calls proceed when Intaris is unreachable. Default is `false` (fail-closed) -- tool calls are blocked when Intaris is down. |
| `INTARIS_INTENTION` | (auto) | Session intention override. Default: `"OpenCode coding session in <cwd>"` |

### 2. Install the Plugin

Copy `intaris.ts` to your OpenCode plugins directory:

```bash
# Global (recommended -- guardrails apply to all projects)
mkdir -p ~/.config/opencode/plugins
cp intaris.ts ~/.config/opencode/plugins/

# Or project-level
mkdir -p .opencode/plugins
cp intaris.ts .opencode/plugins/
```

Local plugins are loaded automatically -- no config entry needed.

### 3. Verify

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

See `opencode.json` in this directory for a complete example.

## How It Works

### Plugin Flow

1. **`session.created`**: Creates an Intaris session via `POST /api/v1/intention` with the working directory as context.
2. **`tool.execute.before`**: Before every tool call:
   - Ensures an Intaris session exists (lazy creation for resumed sessions)
   - Calls `POST /api/v1/evaluate` with the tool name and arguments
   - **approve**: tool executes normally
   - **deny**: throws an error with reasoning (blocks execution)
   - **escalate**: throws an error directing the user to the Intaris UI for approval

### MCP Proxy Flow

1. OpenCode connects to Intaris at `/mcp` as a remote MCP server.
2. `tools/list` returns aggregated tools from all upstream servers.
3. `tools/call` evaluates each call through the safety pipeline before forwarding.

## Tool Name Conventions

When using the **plugin** approach, tool names are passed as-is from OpenCode to Intaris. OpenCode uses these tool names:

- Built-in tools: `read`, `edit`, `write`, `bash`, `glob`, `grep`
- MCP tools: The MCP tool name directly (e.g., `add_memory`, `search_memories`)

When configuring Intaris session policies (fnmatch patterns), use these names:

```json
{
  "policy": {
    "allow": ["read", "glob", "grep"],
    "deny": ["bash"]
  }
}
```

When using the **MCP proxy** approach, tools are namespaced as `server_name:tool_name` (e.g., `mnemory:add_memory`).

## Troubleshooting

- **Tool calls blocked unexpectedly**: Check that `INTARIS_URL` and `INTARIS_API_KEY` are set correctly. Run OpenCode with `--print-logs` to see evaluation decisions.
- **"Cannot create session" errors**: Verify Intaris is running and reachable at the configured URL.
- **All tool calls allowed**: Ensure the plugin is loaded (check for "Plugin initialized" in logs). If using `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable.
- **Slow first tool call**: The first tool call creates an Intaris session (~1-2s) before evaluating. Subsequent calls are faster.
- **Double evaluation**: If you see each tool call evaluated twice, you may have both the plugin and MCP proxy configured. Use only one approach.
