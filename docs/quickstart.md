# Quick Start

Get Intaris running and connected to your first AI agent in 5 minutes.

## Step 1: Start Intaris

Intaris needs an OpenAI-compatible API key for safety evaluation. It picks up `LLM_API_KEY` from your environment automatically.

**uvx (recommended):**

```bash
LLM_API_KEY=sk-your-key uvx intaris
```

**Docker:**

```bash
LLM_API_KEY=sk-your-key docker compose up -d
```

**pip:**

```bash
pip install intaris
LLM_API_KEY=sk-your-key intaris
```

Intaris starts on `http://localhost:8060`. Open `http://localhost:8060/ui` in your browser to see the management dashboard.

## Step 2: Connect Your Client

### OpenCode (Plugin)

The fastest way to get started with [OpenCode](https://opencode.ai):

```bash
export INTARIS_URL=http://localhost:8060
cp integrations/opencode/intaris.ts ~/.config/opencode/plugins/
```

Run OpenCode — every tool call is now evaluated by Intaris before execution.

### Claude Code (Hooks)

For [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
export INTARIS_URL=http://localhost:8060
mkdir -p ~/.claude/scripts
cp integrations/claude-code/scripts/*.sh ~/.claude/scripts/
chmod +x ~/.claude/scripts/*.sh
```

Merge `integrations/claude-code/hooks.json` into your `~/.claude/settings.json`. See the [Claude Code Guide](clients/claude-code.md) for details.

### Any MCP Client (Proxy)

For any MCP-compatible client, point it at Intaris's `/mcp` endpoint:

```json
{
  "mcpServers": {
    "intaris": {
      "type": "streamable-http",
      "url": "http://localhost:8060/mcp"
    }
  }
}
```

Then configure upstream MCP servers in Intaris via the UI or REST API. See the [MCP Proxy Guide](mcp-proxy.md).

## Step 3: Try It

Trigger a tool call from your agent. You should see:

- **Management UI** (`http://localhost:8060/ui`) — sessions, evaluations, and audit records appear in real time
- **Server logs** — evaluation decisions logged with tool name, decision, and latency
- **Agent output** — approved calls execute normally; denied/escalated calls show the reason

## Step 4 (Optional): Add Authentication

By default Intaris accepts all requests. To protect the server:

```bash
# Single shared key
export INTARIS_API_KEY=your-secret-key

# Or multi-key with user mapping (recommended)
export INTARIS_API_KEYS='{"key-for-alice": "alice@example.com", "key-for-bob": "bob@example.com"}'
```

Clients send the key via `Authorization: Bearer <key>` or `X-API-Key: <key>` header. When using a single shared key, clients must also send `X-User-Id` to identify themselves.

See the [Configuration Reference](configuration.md) for all environment variables.

## What's Next

- [Architecture](architecture.md) — Understand how Intaris evaluates tool calls
- [Evaluation Pipeline](evaluation-pipeline.md) — Classification, LLM evaluation, and decision matrix
- [Configuration](configuration.md) — Tune LLM models, timeouts, and rate limits
- [Management UI](management-ui.md) — Monitor sessions and approve escalations
- [MCP Proxy](mcp-proxy.md) — Proxy upstream MCP servers through Intaris
- [Deployment](deployment.md) — Production deployment with Docker and authentication
