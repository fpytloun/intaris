# MCP Proxy

Intaris can act as an MCP proxy, sitting between LLM clients and upstream MCP servers. Every tool call is evaluated through the safety pipeline before being forwarded upstream.

## How It Works

1. **Client connects** to Intaris at `/mcp` using MCP Streamable HTTP transport
2. **`tools/list`** -- Intaris aggregates tools from all configured upstream servers, namespaced as `server_name:tool_name`
3. **`tools/call`** -- Intaris evaluates the call through the full safety pipeline (classify -> LLM -> decide -> audit), then forwards approved calls to the upstream server
4. **Escalated calls** return an `isError: true` result with a message directing the user to the Intaris UI for approval

## Configuration

### Via REST API

Create servers through the REST API or management UI:

```bash
curl -X PUT http://localhost:8060/api/v1/mcp/servers/my-server \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "transport": "streamable-http",
    "config": {
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer upstream-token"}
    },
    "enabled": true
  }'
```

### Via Config File

Define servers in a JSON file and point to it with `MCP_CONFIG_FILE`:

```json
{
  "users": {
    "alice@example.com": {
      "mcpServers": {
        "my-server": {
          "type": "streamable-http",
          "url": "https://example.com/mcp",
          "headers": {"Authorization": "Bearer token"},
          "agent_pattern": "*"
        }
      }
    }
  }
}
```

File-defined servers are stored with `source="file"` in the database. On startup, the config loader reconciles: new servers are inserted, existing ones are updated, and orphaned file-sourced entries are deleted.

### Via Management UI

The Servers tab in the management UI provides a visual interface for adding, editing, and deleting MCP servers, as well as setting per-tool preferences.

<p align="center">
  <img src="../files/screenshots/ui-servers.png" width="800" alt="MCP Servers">
  <br><em>MCP server management with per-tool preference overrides</em>
</p>

## Transports

| Transport | Config Fields | Description |
|---|---|---|
| `stdio` | `command`, `args`, `env` | Subprocess-based. Requires `MCP_ALLOW_STDIO=true`. |
| `streamable-http` | `url`, `headers` | HTTP-based (MCP SDK `streamablehttp_client`). |
| `sse` | `url`, `headers` | Server-Sent Events (MCP SDK `sse_client`). |

### Stdio Example

```json
{
  "transport": "stdio",
  "config": {
    "command": "npx",
    "args": ["-y", "tavily-mcp"],
    "env": {"TAVILY_API_KEY": "tvly-..."}
  }
}
```

### HTTP Example

```json
{
  "transport": "streamable-http",
  "config": {
    "url": "https://mnemory.example.com/mcp",
    "headers": {"Authorization": "Bearer token"}
  }
}
```

## Tool Namespacing

Tools are namespaced as `server_name:tool_name` (colon separator). When a client calls a namespaced tool, the proxy strips the prefix, routes to the correct upstream server, and forwards the original tool name.

Example: Client calls `tavily:tavily_search` -> Intaris evaluates -> forwards `tavily_search` to the `tavily` upstream server.

## Tool Preferences

Per-tool overrides of the default classification behavior. Set via the REST API or management UI.

| Preference | Effect |
|---|---|
| `auto-approve` | Skip LLM evaluation, classify as READ (fast path) |
| `escalate` | Always escalate for human review |
| `deny` | Always deny (classify as CRITICAL) |

Preferences are checked at steps 2-5 of the classification priority chain, before critical patterns and the read-only allowlist.

### Setting Preferences

```bash
# Auto-approve all tavily_search calls
curl -X PUT http://localhost:8060/api/v1/mcp/servers/tavily/preferences/tavily_search \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"preference": "auto-approve"}'

# Always escalate dangerous_tool calls
curl -X PUT http://localhost:8060/api/v1/mcp/servers/my-server/preferences/dangerous_tool \
  -H "Authorization: Bearer your-key" \
  -H "Content-Type: application/json" \
  -d '{"preference": "escalate"}'
```

## Escalation Retry

When a tool call is escalated and later approved by a human, subsequent identical calls (same tool + same args) reuse the approval for 10 minutes. Identity is based on SHA-256 of `json.dumps(args, sort_keys=True, separators=(',', ':'))`.

This means the agent can retry the exact same call after approval without triggering another escalation.

## Session Auto-Creation

The MCP proxy auto-creates sessions for new connections. Intention is resolved in priority order:

1. `X-Intaris-Intention` request header
2. `server_instructions` from the MCP initialize request
3. Default: `"MCP proxy session — evaluate all tool calls for safety"`

## Connecting Clients

### OpenCode

```json
{
  "mcp": {
    "intaris": {
      "type": "remote",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-key",
        "X-Agent-Id": "opencode",
        "X-Intaris-Intention": "OpenCode coding session"
      }
    }
  }
}
```

### Claude Code

```json
{
  "mcpServers": {
    "intaris": {
      "type": "streamable-http",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-key",
        "X-Agent-Id": "claude-code",
        "X-Intaris-Intention": "Claude Code coding session"
      }
    }
  }
}
```

### Any MCP Client

Any client that supports MCP Streamable HTTP transport can connect to `http://localhost:8060/mcp` with appropriate auth headers.

## Encryption

MCP server secrets (API keys in headers, environment variables for stdio) are encrypted at rest using Fernet symmetric encryption. Set `INTARIS_ENCRYPTION_KEY` to enable:

```bash
# Generate a key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Set it
export INTARIS_ENCRYPTION_KEY=your-generated-key
```

The encryption key is required when any MCP server has secrets in its configuration. Without it, creating servers with secrets will fail.

## Connection Management

The `MCPConnectionManager` handles upstream connections:

- **Lazy connections**: Upstream servers are connected on first use
- **Idle timeout**: Connections are closed after 30 minutes of inactivity
- **Per-user limit**: Maximum 10 concurrent connections per user
- **Background sweep**: Periodic cleanup of idle connections
- **Tools cache**: Tool lists are cached for 5 minutes per server

## Tool Name Conventions

Different clients use different naming conventions:

| Client | Built-in Tools | MCP Tools (via proxy) |
|---|---|---|
| OpenCode | `read`, `edit`, `write`, `bash` | `server_name:tool_name` |
| Claude Code | `Read`, `Edit`, `Write`, `Bash` | `server_name:tool_name` |

When using the MCP proxy, all tools are namespaced regardless of the client. Session policies must use the namespaced format.
