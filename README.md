<p align="center">
  <img src="files/banner.jpg" alt="Intaris">
</p>

# intaris

Guardrails service for AI agents. Intaris sits between your AI agent and its tools, evaluating every tool call for safety and alignment before allowing execution. Works with [OpenCode](https://opencode.ai), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenClaw](https://github.com/fpytloun/openclaw/tree/v2026.3.13), and any MCP-compatible client.

**Default-deny.** Every tool call is classified and evaluated. Read-only operations are fast-pathed; everything else goes through LLM safety evaluation. Unknown tools are never auto-approved.

**Real-time.** Sub-second evaluation with a priority-ordered decision matrix. Read-only calls resolve in under 1ms. LLM evaluations complete within the 5-second circuit breaker. WebSocket streaming for live monitoring.

**Self-hosted.** Single Python process, SQLite or PostgreSQL storage, no external dependencies beyond an LLM API key. Your code and audit trail stay under your control.

Part of the [Cognara](https://github.com/fpytloun) platform (Cognis controller, Intaris guardrails, [Mnemory](https://github.com/fpytloun/mnemory) memory).

## Features

- **Default-deny classifier** -- Explicit read-only allowlist with critical pattern detection. Everything not allowlisted goes through LLM evaluation.
- **LLM safety evaluation** -- OpenAI-compatible structured output for alignment checking, risk assessment, and decision reasoning.
- **Priority-ordered decision matrix** -- Critical risk auto-denies, aligned low/medium approves, high risk and misalignment escalate for human review.
- **Session management** -- Hierarchical parent/child sessions with intention tracking, lifecycle states, and idle sweep.
- **Intention tracking** -- User-driven intention model with IntentionBarrier for real-time updates and AlignmentBarrier for parent/child enforcement.
- **MCP proxy** -- Sits between clients and upstream MCP servers, evaluating every tool call with per-tool preference overrides.
- **Audit trail** -- Every evaluation is logged with decision, reasoning, risk level, classification, latency, and redacted arguments.
- **Secret redaction** -- API keys, passwords, tokens, and connection strings are automatically redacted before audit storage.
- **Filesystem path protection** -- Working directory enforcement with approved path prefix learning from LLM approvals.
- **Session recording** -- Full-fidelity event logs with live tailing, playback, and chunked ndjson storage (filesystem or S3).
- **Behavioral analysis** -- Three-layer system: per-call data collection, session summaries, and cross-session behavioral profiling.
- **Management UI** -- Built-in web dashboard with session tree view, audit log, approval queue, MCP server management, and real-time charts.
- **Judge auto-resolution** -- Escalated tool calls can be automatically reviewed by a more capable LLM (gpt-5.4), reducing human intervention while maintaining safety. Three modes: disabled, auto, advisory.
- **Webhook callbacks** -- HMAC-signed escalation notifications for external approval systems.
- **Notification channels** -- Per-user push notifications (Pushover, Slack, webhook) with one-click approve/deny action links.
- **Rate limiting** -- Per-session sliding window rate limiter to prevent runaway agents.

## Quick Start

Intaris needs an OpenAI-compatible API key for safety evaluation. It picks up `LLM_API_KEY` from your environment automatically.

```bash
LLM_API_KEY=sk-your-key uvx intaris
```

That's it. Intaris starts on `http://localhost:8060`, management UI at `http://localhost:8060/ui`.

Now integrate with your agent. We already ship extensions for some clients. For
example for **OpenCode**, install the plugin:

```bash
export INTARIS_URL=http://localhost:8060
cp integrations/opencode/intaris.ts ~/.config/opencode/plugins/
```

Intaris can also serve as MCP proxy with audit trail and guardrails for tool calls. To use that, configure **any MCP client** to use intaris as a single MCP server:

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

And add MCP servers via Intaris UI or config.

Intaris is also available via [Docker, pip, or production setup](docs/deployment.md). See the [full quick start guide](docs/quickstart.md) for more clients and options.

## Screenshots

<p align="center">
  <img src="files/screenshots/ui-dashboard.png" width="800" alt="Dashboard">
  <br><em>Dashboard -- evaluation metrics, decision distribution, performance stats, and activity timeline</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-sessions.png" width="800" alt="Sessions">
  <br><em>Sessions -- hierarchical tree view with expandable session details and recent evaluations</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-approvals.png" width="800" alt="Approvals">
  <br><em>Approvals -- pending escalations with reasoning, arguments, and one-click approve/deny</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-analysis-profile.png" width="800" alt="Behavioral Profile">
  <br><em>Analysis -- behavioral risk profile with per-agent risk indicators and trends</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-analysis-trend.png" width="800" alt="Analysis Trend">
  <br><em>Analysis -- cross-session behavioral trend tracking over time</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-session-suspicious.png" width="800" alt="Suspicious Session">
  <br><em>Sessions -- suspicious session detail with evaluation reasoning and risk assessment</em>
</p>

<p align="center">
  <img src="files/screenshots/ui-critical-denied.png" width="800" alt="Critical Denied">
  <br><em>Audit -- critical tool execution denied with detailed reasoning</em>
</p>

See the [Management UI docs](docs/management-ui.md) for all tabs and features.

## Supported Clients

| Client | Integration | Setup Guide |
|---|---|---|
| [OpenCode](https://opencode.ai) | Plugin (`intaris.ts`) | [OpenCode Guide](docs/clients/opencode.md) |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Hooks (bash scripts) | [Claude Code Guide](docs/clients/claude-code.md) |
| [OpenClaw](https://github.com/fpytloun/openclaw/tree/v2026.3.13) | Extension (`@openclaw/intaris`) | [OpenClaw Guide](docs/clients/openclaw.md) |
| Any MCP client | MCP proxy (`/mcp` endpoint) | [MCP Proxy Guide](docs/mcp-proxy.md) |

**Plugin/Hooks** give fine-grained control: custom error messages, fail-open/fail-closed behavior, session lifecycle management, and behavioral analysis. **MCP proxy** is zero-code configuration but with less UX control.

## How It Works

**Intercept.** The client integration (plugin, hooks, or MCP proxy) captures every tool call before execution and sends it to Intaris for evaluation.

**Classify.** The classifier checks the tool against a priority chain: session policy denies, tool preference overrides, critical patterns, the read-only allowlist, and filesystem path policy. Read-only tools are auto-approved. Critical patterns are auto-denied.

**Evaluate.** Tool calls classified as WRITE go through LLM safety evaluation. The LLM assesses alignment with the session intention, risk level (low/medium/high/critical), and recommends a decision -- all within a 4-second timeout.

**Decide.** The decision matrix applies priority-ordered rules: critical risk always denies, aligned low/medium risk approves, high risk and misalignment escalate for human review. The decision, reasoning, and full context are recorded in the audit trail.

See the [Architecture](docs/architecture.md) and [Evaluation Pipeline](docs/evaluation-pipeline.md) docs for the full technical details.

## Benchmark Results

Intaris catches 100% of critical threats (destructive commands, data exfiltration, RCE) with near-zero false positives (0.4% FPR). Across 41 benchmark scenarios including adversarial attacks, social engineering, and cross-session patterns, Intaris achieves 83% F1 with 98% precision -- meaning it almost never blocks legitimate developer work.

| Metric | Value |
|---|---|
| Precision | 97.9% |
| F1 Score | 83.2% |
| False Positive Rate | 0.4% |
| Critical Misses | 0 |
| Avg Latency | 1.7s |

See the [Benchmarking docs](docs/benchmarking.md) for methodology, scenario details, and how to run your own benchmarks.

## Documentation

| Document | Description |
|---|---|
| [Quick Start](docs/quickstart.md) | Get running in 5 minutes |
| [Architecture](docs/architecture.md) | System design, layers, and key decisions |
| [Evaluation Pipeline](docs/evaluation-pipeline.md) | Classification, LLM evaluation, and decision matrix |
| [Configuration](docs/configuration.md) | Environment variable reference |
| [REST API](docs/rest-api.md) | Full API endpoint reference |
| [MCP Proxy](docs/mcp-proxy.md) | MCP proxy setup, tool namespacing, and preferences |
| [Management UI](docs/management-ui.md) | Built-in web dashboard |
| [Deployment](docs/deployment.md) | Production deployment guide |
| [Development](docs/development.md) | Contributing, tests, and code conventions |
| [OpenCode Integration](docs/clients/opencode.md) | OpenCode plugin setup |
| [Claude Code Integration](docs/clients/claude-code.md) | Claude Code hooks setup |
| [OpenClaw Integration](docs/clients/openclaw.md) | OpenClaw extension setup |
| [Benchmarking](docs/benchmarking.md) | Guardrails benchmark system |

## License

Business Source License 1.1 — see [LICENSE](LICENSE) for the full text.

The Licensed Work is (c) 2026 Filip Pytloun. You may use the Software for your own internal business operations free of charge. Commercial use (SaaS, managed services, or as a component of a commercial product) requires a separate license. On the Change Date (2030-03-15), the license converts to Apache License 2.0.

For alternative licensing arrangements, contact: filip@pytloun.cz
