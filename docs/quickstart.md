# Quick Start

Get Intaris running and connected to your first AI coding agent in 5 minutes.

## Prerequisites

- Python 3.11 or later
- An OpenAI-compatible LLM API key (OpenAI, Azure OpenAI, Anthropic via proxy, local models, etc.)

## 1. Install

**pip (recommended for development):**

```bash
pip install -e .
```

**Docker:**

```bash
docker compose up -d
```

**From source with uv:**

```bash
uv pip install -e ".[dev]"
```

## 2. Configure

The only required configuration is an LLM API key:

```bash
export LLM_API_KEY=sk-your-key
```

Optionally, set an API key to protect the Intaris server:

```bash
# Single shared key (clients must also send X-User-Id header)
export INTARIS_API_KEY=your-secret-key

# Or multi-key with user mapping (recommended)
export INTARIS_API_KEYS='{"key-for-alice": "alice@example.com", "key-for-bob": "bob@example.com"}'
```

See the [Configuration Reference](configuration.md) for all environment variables.

## 3. Start the Server

```bash
intaris
```

The server starts at `http://localhost:8060`. Open `http://localhost:8060/ui` in your browser to see the management dashboard.

Verify it's running:

```bash
curl http://localhost:8060/health
```

## 4. Connect a Client

### OpenCode (Plugin)

The fastest way to get started with [OpenCode](https://opencode.ai):

```bash
# Set environment variables
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-secret-key    # if you set one above
export INTARIS_USER_ID=your-username      # required with single-key mode

# Install the plugin
mkdir -p ~/.config/opencode/plugins
cp integrations/opencode/intaris.ts ~/.config/opencode/plugins/
```

Run OpenCode -- every tool call is now evaluated by Intaris. See the [OpenCode Guide](clients/opencode.md) for full details.

### Claude Code (Hooks)

For [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
# Set environment variables
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-secret-key
export INTARIS_USER_ID=your-username

# Install hooks and scripts
mkdir -p ~/.claude/scripts
cp integrations/claude-code/scripts/*.sh ~/.claude/scripts/
chmod +x ~/.claude/scripts/*.sh
cp integrations/claude-code/hooks.json ~/.claude/settings.json
```

See the [Claude Code Guide](clients/claude-code.md) for full details.

### Any MCP Client (Proxy)

For any MCP-compatible client, point it at Intaris's `/mcp` endpoint:

```json
{
  "mcpServers": {
    "intaris": {
      "type": "streamable-http",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-secret-key",
        "X-User-Id": "your-username"
      }
    }
  }
}
```

Then configure upstream MCP servers in Intaris via the UI or REST API. See the [MCP Proxy Guide](mcp-proxy.md) for full details.

## 5. Verify

Once connected, trigger a tool call from your agent and check:

1. **Server logs** -- You should see evaluation decisions being logged
2. **Management UI** -- Open `http://localhost:8060/ui` to see sessions, evaluations, and audit records
3. **Client output** -- The agent should report approved/denied decisions

## What's Next

- [Architecture](architecture.md) -- Understand how Intaris evaluates tool calls
- [Evaluation Pipeline](evaluation-pipeline.md) -- Deep dive into classification and decision logic
- [Configuration](configuration.md) -- Tune LLM models, timeouts, and rate limits
- [Management UI](management-ui.md) -- Monitor sessions and approve escalations
- [MCP Proxy](mcp-proxy.md) -- Proxy upstream MCP servers through Intaris
- [Deployment](deployment.md) -- Production deployment with Docker and authentication
