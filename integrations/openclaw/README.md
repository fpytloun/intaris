# OpenClaw Plugin -- Intaris

An [OpenClaw](https://openclaw.ai) plugin that evaluates every tool call through [Intaris](https://github.com/fpytloun/intaris) safety pipeline before allowing execution.

## Features

- **Tool Call Guardrails**: Every tool call is evaluated via `POST /api/v1/evaluate` before execution
- **Session Management**: Creates/manages Intaris sessions mirroring OpenClaw agent sessions, including sub-agent linking
- **Escalation Handling**: Escalated tool calls enter a polling loop waiting for human approval via the Intaris UI
- **Session Recording**: Optional audit trail recording of all messages, tool calls, and results
- **MCP Tool Proxy**: Fetches and registers MCP tools from Intaris-connected MCP servers
- **Reasoning Context**: Forwards user prompts and assistant responses to Intaris for informed safety decisions
- **Fail-open/Fail-closed**: Configurable behavior when Intaris is unreachable

## Prerequisites

A running [Intaris](https://github.com/fpytloun/intaris) server accessible via HTTP.

## Install

```bash
openclaw plugins install @fpytloun/openclaw-intaris
```

## Configure

In your `openclaw.json`:

```json5
{
  plugins: {
    entries: {
      intaris: {
        enabled: true,
        config: {
          url: "http://localhost:8060",
          apiKey: "${INTARIS_API_KEY}"
        }
      }
    }
  }
}
```

### Configuration Options

| Key | Env Var | Default | Description |
|-----|---------|---------|-------------|
| `url` | `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `apiKey` | `INTARIS_API_KEY` | (empty) | API key for authentication |
| `userId` | `INTARIS_USER_ID` | (empty) | User ID (optional if API key maps to user) |
| `failOpen` | `INTARIS_FAIL_OPEN` | `false` | Allow tool calls when Intaris is unreachable |
| `allowPaths` | `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent directories for policy allow_paths |
| `escalationTimeout` | `INTARIS_ESCALATION_TIMEOUT` | `0` | Max seconds to wait for escalation (0 = no timeout) |
| `checkpointInterval` | `INTARIS_CHECKPOINT_INTERVAL` | `25` | Evaluate calls between checkpoints (0 = disabled) |
| `recording` | `INTARIS_SESSION_RECORDING` | `false` | Enable session recording |
| `recordToolOutput` | `INTARIS_RECORD_TOOL_OUTPUT` | (follows recording) | Record full tool output in events |
| `recordingFlushSize` | `INTARIS_RECORDING_FLUSH_SIZE` | `50` | Events per recording batch |
| `recordingFlushMs` | `INTARIS_RECORDING_FLUSH_MS` | `10000` | Recording flush interval in ms |
| `mcpTools` | `INTARIS_MCP_TOOLS` | `true` | Enable MCP tool proxy |
| `mcpToolsCacheTtlMs` | `INTARIS_MCP_TOOLS_CACHE_TTL_MS` | `900000` | MCP tool list cache TTL in ms (15 min) |

## How It Works

1. **`session_start`**: Creates an Intaris session via `POST /api/v1/intention`
2. **`subagent_spawning`**: Links child Intaris session to parent before sub-agent starts
3. **`before_agent_start`**: Forwards user prompt as reasoning context to Intaris
4. **`before_tool_call`**: Evaluates every tool call via `POST /api/v1/evaluate`
   - **approve**: tool executes normally
   - **deny**: returns `{ block: true, blockReason }` (blocks execution)
   - **escalate**: polls for user decision, blocks until resolved
5. **`after_tool_call`**: Records tool results for audit trail
6. **`llm_output`**: Captures last assistant text for intention context
7. **`agent_end`**: Transitions session to idle, sends checkpoints
8. **`session_end`**: Signals session completion to Intaris

## License

MIT
