# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.2] - 2026-03-28

### Added

- **Root redirect** -- Navigating to `/` now redirects to `/ui/` for better discoverability of the management UI.
- **Cognis Stage 0 prerequisites** -- JWT authentication (ES256), event store extensions (lifecycle events, idempotent appends), and session metadata fields for Cognis controller integration.
- **Ex-post denial overrides** -- Users can now approve tool calls that were denied by the critical classifier or LLM evaluation, with escalation retry support for agent retries within 10 minutes.
- **Audit resolution actions** -- Approve/deny actions directly from expanded audit records in the UI.

### Fixed

- **Auth logging** -- Reduced per-request auth resolution log messages from INFO to DEBUG to eliminate log spam.
- **OpenCode plugin** -- Forward web-mode user messages to the reasoning endpoint for proper intention tracking.

## [0.3.1] - 2026-03-26

### Added

- **Judge notification event types** -- Dedicated event types (`judge_denial`, `judge_approval`, `judge_deferral`, `judge_error`) for judge auto-resolution outcomes. Initial escalation notification is deferred when judge is enabled, replaced by a single judge outcome notification. Backward-compatible fallback mapping for existing notification channels.

### Fixed

- **Prompts** -- Enforce English-only output across all LLM system prompts (evaluator, judge, intention, analysis) to prevent non-English responses when processing non-English user content.

## [0.3.0] - 2026-03-26

### Added

- **Judge auto-resolution** -- Escalated tool calls can be automatically reviewed by a more capable LLM (gpt-5.4), reducing human intervention while maintaining safety. Three modes: `disabled` (default), `auto`, `advisory`. Deny-if-uncertain in auto mode. Shared `resolve_with_side_effects()` handler ensures identical side effects for human and judge resolution paths. Human users can override any judge decision via the UI; human decisions are final.
- **Judge enriched context** -- Judge receives full reasoning records (up to 8000-char safety valve) with associated context metadata. Sub-session parent context included for cross-session visibility. Judge reasoning stored and used in resolution notifications.
- **OpenClaw plugin** -- Full `@fpytloun/openclaw-intaris` extension with 10 hooks (`session_start`, `before_tool_call`, `after_tool_call`, `before_agent_start`, `llm_output`, `subagent_spawning`, `subagent_ended`, `agent_end`, `before_reset`, `session_end`), MCP tool factory, session recording, sub-agent support, and npm publish CI workflow.
- **Claude Code hooks overhaul** -- Major rewrite for feature parity with OpenCode plugin: shared library (`intaris-lib.sh`), sub-agent support (`intaris-subagent.sh`, `intaris-subagent-stop.sh`), prompt injection hook (`intaris-prompt.sh`), stop-failure handler, and session recording via `PostToolUse`.
- **Budget-aware child compression** -- Parent summaries compress lower-risk child sessions when the child budget (`_MAX_CHILD_CHARS`) is exceeded, with observability metrics (`summary_child_compressed_count`, `summary_child_overflow_total`).

### Changed

- **Default LLM models** -- Updated to gpt-5.4 generation: `gpt-5.4-nano` (evaluate), `gpt-5.4-mini` (analysis), `gpt-5.4` (L3/judge/benchmark).

### Fixed

- **Security** -- Prevent sensitive data leaks in log output across alignment, streaming, background, config, intention, LLM, MCP client, and sanitize modules.
- **LLM** -- Harden JSON key handling with alias remapping and summary validation.
- **Notifications** -- Budget-aware formatting to resolve truncation issues. Add `agent_id` to escalation, denial, suspension, and resolution messages. Use judge reasoning in resolution notifications.
- **Evaluation** -- Suppress contradictory reasoning in user-approved escalation history to prevent LLM confusion.
- **UI** -- Make user override of judge decisions more visible in approvals tab with "overridden by user" indicator.
- **Benchmarks** -- Resolve hierarchical scenario parent sessions. Refresh benchmark results and roadmap notes.
- **Tests** -- Update stale assertion to match current prompt wording. Add LLM JSON key validation and alias remapping tests.

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

[0.3.1]: https://github.com/fpytloun/intaris/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/fpytloun/intaris/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/fpytloun/intaris/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fpytloun/intaris/releases/tag/v0.1.0
