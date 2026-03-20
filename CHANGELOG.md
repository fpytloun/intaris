# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-20

### Added

- **Benchmark system** -- Built-in guardrails benchmark at `tools/benchmark/` with 41 scenarios across 9 categories (adversarial, infrastructure, coding, social engineering, hierarchical, cross-session). Two-track approach: scripted scenarios for deterministic testing and LLM-driven generative scenarios for realistic patterns. Auto-evaluate after run with actionable findings and configuration recommendations.
- **MCP REST API endpoints** -- `GET /api/v1/mcp/tools` and `POST /api/v1/mcp/call` for listing and calling upstream MCP tools via REST (used by OpenClaw's tool factory).
- **MCP eager startup** -- MCP connections are established eagerly on server start with per-server cache isolation for npx/uvx, preventing cache corruption from concurrent installs.
- **Intention tracking improvements** -- Server-side tracking for user-message arrival (`intention_pending` deprecation), cancel-and-restart guard for timestamp cleanup, improved intention prompt with full LLM output preservation.
- **OpenClaw integration** -- Full integration docs and AGENTS.md support for the `@openclaw/intaris` extension ([OpenClaw fork v2026.3.13](https://github.com/fpytloun/openclaw/tree/v2026.3.13/extensions/intaris)).
- **Docker improvements** -- Added uvx and Node.js to Docker image for stdio MCP server support.

### Fixed

- **Session lifecycle** -- Keep parent session active while children are running.
- **MCP proxy** -- Resolve `agent_id` from session context in `/call` endpoint.
- **Intention** -- Ignore client intention updates in PATCH endpoint (user-driven only). Guard timestamp cleanup in cancel-and-restart path.
- **Audit** -- Use case-insensitive substring match for tool name filter.
- **UI** -- Dashboard charts not refreshing on agent switch; persist agent selection. Render OpenClaw exec tool calls with input/output in Console. Render assistant messages in Console view for OpenClaw source. Various chart, filter, sorting, and player improvements.
- **API** -- Require `agent_id` on session creation. Fix real-time recording.

### Changed

- Improved benchmark evaluator with enriched scenarios and actionable findings report.

## [0.1.0] - 2026-03-15

Initial release.

### Added

- **Core evaluation pipeline** -- Default-deny classifier with read-only allowlist, critical pattern detection, LLM safety evaluation, and priority-ordered decision matrix.
- **Session management** -- Hierarchical parent/child sessions with intention tracking, lifecycle states (active, idle, completed, suspended, terminated), and idle sweep.
- **Intention model** -- User-driven intention with IntentionBarrier for real-time updates and AlignmentBarrier for parent/child enforcement.
- **MCP proxy** -- Transparent proxy between clients and upstream MCP servers with per-tool preference overrides, escalation retry, and tool namespacing.
- **Audit trail** -- Every evaluation logged with decision, reasoning, risk level, classification, latency, and redacted arguments. Multiple record types (tool_call, reasoning, checkpoint, summary).
- **Secret redaction** -- Pattern-based and key-name-based redaction of API keys, passwords, tokens, and connection strings before audit storage.
- **Filesystem path protection** -- Working directory enforcement with approved path prefix learning from LLM approvals and user-approved escalations.
- **Session recording** -- Full-fidelity event logs with live tailing via WebSocket, session playback, and chunked ndjson storage (filesystem or S3).
- **Behavioral analysis** -- Three-layer system: L1 per-call data collection, L2 session summaries with hierarchical support and compaction, L3 cross-session behavioral profiling with progressive summarization.
- **Management UI** -- Built-in web dashboard (Alpine.js + Tailwind CSS) with 6 tabs: Dashboard, Sessions, Audit, Approvals, Servers, Settings. Real-time WebSocket updates.
- **Webhook callbacks** -- HMAC-SHA256 signed escalation notifications for external approval systems.
- **Notification channels** -- Per-user push notifications (Pushover, Slack, webhook) with one-click approve/deny action links.
- **Rate limiting** -- Per-session sliding window rate limiter.
- **Background worker** -- SQLite-backed task queue with retry, exponential backoff, and duplicate detection for analysis tasks.
- **Client integrations** -- OpenCode plugin (`intaris.ts`) and Claude Code hooks (bash scripts).
- **Documentation** -- Architecture, evaluation pipeline, configuration, REST API, MCP proxy, management UI, deployment, development, and client integration guides.

[0.2.0]: https://github.com/fpytloun/intaris/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fpytloun/intaris/releases/tag/v0.1.0
