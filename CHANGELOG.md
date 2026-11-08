# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.15.1] - 2026-11-08

### Fixed

- **Operation-aware disclosure checks** -- Distinguish secret-bearing Git output
  from credential-related branch names, commit messages, authentication settings,
  and filename-only diffs. Follow the child command of an `env` wrapper instead
  of treating development filenames as disclosed values. Actual secret reads
  retain disclosure checks; task restrictions and explicit denies still apply.
- **Disclosure review explanations** -- Explain when disclosure authorization
  remains unverified even if an automated reviewer recommends task approval.

## [0.15.0] - 2026-11-01

### Added

- **Independent session participation** -- Sessions can disable behavioral
  analytics and proactive guardrails independently. Recording and explicit
  tool evaluation remain available; queued and in-flight derived work is
  fenced against opt-out, including disable/re-enable races.

- **Storage observability** -- Expose append latency/failures, pending payload
  bytes and persisted backlog age, actual chunk distributions, and per-operation
  read decode amplification through existing metrics diagnostics. Stream metrics
  preserve cancellation and exclude consumer waiting time; scrapes add no storage
  I/O or payload-derived labels. Existing S3 and cache metrics remain available.

### Changed

- **Session archive efficiency** -- New filesystem and S3 chunks use checksummed
  Zstandard level 1 by default. Set `EVENT_STORE_COMPRESSION=none` for uncompressed
  writes. Readers support both formats; upgrade all readers before enabling
  compression, and do not downgrade while compressed history remains.
- **Event JSON performance** -- Use `msgspec` for recording payloads and spool
  JSON, preserving legacy value semantics and established retry/hash identities.

### Fixed

- **Alignment retry fencing** -- Use the alignment reviewer's database for
  participation checks even when optional evaluator analytics are not configured.
- **Offline benchmark integrity** -- Refresh source-context fixture identities
  after public-content reconstruction without claiming renewed model evidence
  or authorizing live execution.
- **Intention source fidelity** -- Keep diagnostic blocks out of authority
  quotations, repair stale prose without dropping validated restrictions, and
  preserve source paragraph boundaries and title-only updates.
- **Verified script context** -- Bind executor-observed script digests to the
  exact evaluated command; paths alone cannot satisfy hash-conditioned grants.
- **Git search classification** -- Treat search patterns as text while retaining
  checks on pattern files, target paths, and executable pager options.
- **Startup recovery responsiveness** -- Reuse participation snapshots per tenant
  during parent summary recovery and index descendant traversal. Run recovery
  scans off the API event loop, with progress logs and cooperative shutdown,
  so large session histories do not prevent the service from starting.
- **Operator policy review** -- Preserve configured allow/deny effects and all
  allowance conditions in reviewer instructions without treating policy clauses
  as original user speech.
- **Session read amplification** -- Resume audit projection from its durable
  checkpoint and traverse chunks once during export, filtered forward reads and
  backfill instead of repeatedly parsing the same payload.
- **Unattended review failures** -- Apply session resolution policy when an
  automated restriction claim cannot be verified. Policy blocking retains the
  validation-failure cause and unknown risk rather than offering unavailable
  human review. Interactive review and technical-failure handling are unchanged.

## [0.14.1] - 2026-10-23

### Fixed

- **Input budgeting** -- Preserve the largest fitting excerpt of oversized
  evaluation inputs rather than collapsing them to a fixed short view. Partial
  views remain marked and cannot establish affirmative grants.
- **Alignment recovery** -- Recheck misaligned children when the parent's
  intention changes. Relaxed profiles avoid unnecessary alignment reviews for
  controller-proven non-sensitive reads; restrictions and write checks remain.
- **Shell effect classification** -- Parse curl option values and separate
  transfers, GET query parameters, multiline commands, subshells and groups more
  accurately while retaining conservative handling of unsupported syntax.
  GET negation is applied in option order for each transfer.
- **Judge review** -- Present original sources chronologically and mark the
  latest user message. Balanced and bounded-autonomy profiles may resolve
  routine low-risk deferrals without a human, but not consequential actions,
  restriction conflicts, or plan/read-only turns. Failed restriction-quote
  repair retains citable instructions and cannot remove a real prohibition.
- **Executed script context** -- Include bounded, redacted script text from
  successful client-recorded writes earlier in the same session. The text is
  explicitly unverified context, never permission or proof of file integrity.
- **Benchmark portability** -- Require an explicitly configured routing owner
  for read-only transport recovery; remove deployment associations from
  historical reports and use reserved domains in command examples. Historical
  model measurements were not rerun for these content changes.

## [0.14.0] - 2026-10-22

### Added

- **Crash-durable event spool** -- A default-enabled local spool preserves acknowledged
  event batches across process/container crashes, with duplicate-safe sequence
  reservations, pending-event reads, immutable archive uploads and restart recovery.
  File and directory fsync are enabled by default; `EVENT_STORE_SPOOL_FSYNC=false`
  explicitly trades host-crash durability for lower append latency.
  See [local event spool](docs/event-spool.md) for storage, single-process ownership,
  archive-outage admission gates and drain-before-rollout requirements.
- **Inference accounting attribution** -- Optional content-free operation, session,
  task and request correlation headers for Cognis-managed inference; standalone
  providers do not receive them.
- **Public container images** -- Publish multi-architecture server images to
  `ghcr.io/fpytloun/intaris` for main and stable releases.

### Fixed

- Preserve resolver, exact approval scope and intention eligibility in analytics;
  merge event/audit messages without hiding restrictions and retain bounded read targets.
- Keep fresh windows after compaction, track late evidence and resolution updates,
  refresh parent dependencies, and attribute child activity to its direct actor.
- Validate L3 citations against rendered evidence, report coverage gaps, and prevent
  incomplete clean results from clearing actionable profiles.
- Deliver complete summary findings to notifications and supply fresh bounded
  analytics evidence only to existing Judge reviews, without changing L1 or routing.
  See [analytics evidence and limitations](docs/analytics-evidence.md).
- **Evaluation source fidelity** -- Supply original task instructions to first-pass
  evaluation, keep generated intent subordinate to original user scope, and assess
  child alignment as compatibility rather than strict containment.
- **Fact-first policy review** -- Separate factual assessment from controller policy
  routing, recover diffuse-risk abstentions, and preserve fail-closed restriction
  retries and refusal provenance without treating invalid restrictions as missing
  user permission.
- **Disclosure authorization** -- Preserve the requirement for an explicit scoped
  user grant when a Judge lowers the severity of observed credential disclosure.
  Severity-only review cannot authorize printing secrets.
- **Capacity recovery** -- Refresh unknown model capacity before constructing a
  partial inference view, and retain exact mixed-input fallback references when
  no captured user evidence exists.
- **Recovered history and bounded reads** -- Tolerate recovered event timestamps,
  retain evidence coverage when bounding analytics reads, and add an ordered
  tenant audit-pagination index.
- **Spool recovery fencing** -- Preserve archive-read recovery gates and fence
  incomplete recording deletion so accepted events cannot be resurrected.
- **Source distribution hygiene** -- Exclude private deployment automation from
  source distributions, including builds made from a private checkout.
- **Public examples** -- Use reserved domains for reconstructed benchmark contacts
  and remove record-local references and captured confirmation wording from reports.
  Historical model measurements were not rerun for these example changes.

## [0.13.0] - 2026-10-19

### Release status

- This release consolidates the hybrid evaluation and source-context work below. The earlier `0.12.0` candidate was not published on GitHub; managed inference changes from that candidate are included.
- The last complete offline run passed 4,423 tests with 512 skipped and two warnings before the later queue, context-budget and Judge fixes. Those later changes have focused regression/review evidence, not a fresh combined full-suite result. Repeating the suite and adding new tests were explicitly waived for finalization.
- Historical model benchmark reports retain their original cohorts, unsafe observations and limitations. They are not a qualification of this exact final release and do not establish universal semantic accuracy.

### Added

- **Single-call rich intention** -- Generate the session title, summary and a controller-validated structured intention (requests, restrictions, approvals, revocations, uncertain items, context) in one model call with `SOURCE_CONTEXT_GENERATION=single`; quotes are verified against the source user message, bare confirmations become approvals only for a single verified asked-about action, and permissions never become current requests. Persisted in `sessions.intention_profile` with per-item validation drop codes.
- **Per-model L1 input projection** -- `L1_INPUT_PROJECTION=auto|json|lines`; the `lines` projection renders the proposed action first and the user's requests and restrictions as exact quotes grouped by source message, with strict JSON-schema output for `gpt-oss` evaluators on Groq. `auto` selects `lines` for Jev and `gpt-oss` models, the projection each was measured with, and `json` for other chat models.
- **User-simulator Judge review** -- Hybrid escalations are reviewed by a Judge that answers as the user (`approve | ask | refuse`, risk, confidence, basis, citations and a concrete question when needed). Judge can correct model and heuristic classifications, including critical-risk labels and negative child-alignment assessments; original evidence stays audited. Exact configured prohibitions, read-only requirements, human checkpoints, action freshness and tenant boundaries remain enforced.
- **`bounded_autonomy` profile semantics** -- Approve directly a consequential action the user requested (explicit or implicit authorization, within scope, granted by the user) or a validated approval covers, and read-only actions with controller-known effects, when no other obligation is open; route the rest to Judge review, whose `ask` and non-critical `refuse` reach a human.
- **Controller effect witnesses** -- Derive `delete`, `transmit`, `write` or `read` effects from the tool-name verb for namespaced tools (`name_heuristic` provenance), and a `disclose` review signal for emit-style shell commands with secret-bearing operands (`secret_argument` provenance). Original observations remain audited even when Judge corrects their severity.
- **Evaluation redesign documentation** -- `docs/how-intaris-decides.md`, `docs/why-we-rebuilt-intaris-evaluation.md` and `docs/evaluation-redesign-2026-10.md`: the pipeline, motivation and measurements. In the complete Jev comparison (258 required observations per arm across three seeds), rich intention with `bounded_autonomy` reduced benign human escalations from 27 to 21 and raised benign approvals from 96 to 102 against the summary/balanced reference on the same experimental runtime. Two harmful approvals failed the full-run safety gate. A subsequent disclosure fix passed a separate targeted regression; these are not combined into a full-run pass.
- **GPT-OSS-20B remeasurement** -- Under the redesigned pipeline with `bounded_autonomy`, 258 verified observations yielded 112/123 benign approvals, 11 benign escalations and 2/129 unsafe approvals in the known forged-push-permission class. The preregistered historical thresholds passed, but the different historical cohort is not a matched proof of compatibility for every installation. See the measurement report for latency, cost and the no-user fallback limitation.
- **Read-only rich decision evidence UI** -- Session API and UI expose intention items, current tasks, scoped approvals and validation drop codes, alongside active profiles, compiled rules and advisory clauses. Approvals show structured Judge recommendations with exact quoted citations and Jev abstentions; audit details show effect witnesses, L1 projection and explicitly waived operator clauses.

### Changed

- **Balanced defaults and delegated policy** -- New sessions default to hybrid Balanced evaluation. Delegated sessions inherit parent restrictions and effective policy rather than silently reverting to Legacy.
- **Bounded model input** -- Oversized dynamic input uses source-aware excerpts followed by marked middle truncation, preserving original audit records and citation identities. Partial views cannot establish explicit or scoped approval grants.
- **Judge instruction chronology** -- Current relevant user scope takes precedence over superseded task summaries. Assistant blocker reports and automated denials are historical context, not user prohibitions; unchanged restrictions and actual human refusals remain binding.
- **Hybrid `balanced` profile** -- Denials derived from a model label (restriction, target or intent conflict) are reviewed by the Judge instead of being terminal; deterministic deny rules remain terminal.
- **Rich context fallback** -- Sessions without user messages or human notes evaluate with the production context instead of an unavailable rich profile.
- **Recommended evaluator for the hybrid policy** -- Jev with the `lines` projection is the measured, recommended L1 evaluator for the `hybrid_v1` configuration; `gpt-oss-20b` remains supported via `EVALUATOR_BACKEND=llm` and receives the lines projection under `auto`.
- **Free-form session policy compatibility** -- Hybrid activation retains unsupported English clauses as advisory operator policy instead of rejecting Cognis sessions. L1 and Judge receive the text as data, separate from user authority. Unresolved deny clauses require applicability assessment on reads and writes. An applicable deny established by L1 cannot be cleared by a Judge approval that omits its ID. Strict applicable denies require a human; non-strict operator refusals escalate unless explicitly waived by an operator outcome cap, with clause IDs audited. Known compiled-grammar limitation: "Never push to main" remains advisory.

### Fixed

- **Model timeout recovery** -- A timed-out transport attempt retains the remaining shared evaluation retry budget; standalone calls remain bounded.
- **Native MCP review errors** -- Preserve structured retryability and non-dispatch metadata for unavailable reviews instead of flattening them into an unstructured error.
- **Analysis queue recovery and wakeups** -- Fence claims, preserve fresh queued work across restart, share commit-aware wakeups, and bound lease-recovery scans while draining backlogs.
- **Event ingestion diagnostics** -- Retain uncertain appends, distinguish rejected input from temporary failures, and expose bounded operational metrics.
- **Portable release examples** -- Replace unresolved operational references with synthetic fixtures without presenting reconstructed examples as unchanged historical measurements.
- **Async evaluation contention** -- Backend metadata is refreshed by one lazy, bounded worker shared by synchronous and asynchronous callers. HTTP and MCP evaluations wait outside request-worker slots and resume the same decision flow, preserving fast paths, evidence and fail-closed errors without replaying side effects. Queue-depth sampling runs off the event loop and retains ownership of an outstanding query through cancellation and restart.
- **Jev answer validation** -- A Jev label that is not the highest-probability option is treated as an abstention under the normal confidence thresholds instead of aborting the evaluation with an HTTP error; exact ties no longer raise, and residual validation failures escalate instead of erroring.
- **Intention title** -- The single-call prompt no longer invites the literal title "English".

## [0.12.0] - 2026-10-04

### Added

- **Cognis-managed inference** -- Route evaluation, Judge review, analysis, deep analysis, and embeddings through Cognis using service JWT credentials read on each request, supporting credential rotation without restart.

### Fixed

- **Managed reasoning effort** -- Inherit Cognis routing defaults unless reasoning effort is explicitly configured, while preserving standalone defaults and explicit client overrides.

### Changed

- **OpenAI client dependency** -- Require OpenAI Python client `2.26.0` or newer for managed inference support.
- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.12.0`.

## [0.11.0] - 2026-09-27

### Added

- **Jev evaluation backend** -- Support Jev through the OpenRouter Decisions API as an alternative tool-call evaluator, with outcome-aware confidence calibration and configuration documentation.
- **Session policy outcome ceiling** -- Limit the most permissive evaluation outcome per session and retain the policy decision in audit provenance.
- **Evaluator benchmarks** -- Document production-model comparisons, Jev calibration, and the resulting evaluator assessment.

### Fixed

- **Unattended escalations** -- Deny evaluations that require escalation when no human approval channel is available, and allow Judge review before resolving unattended escalations.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.11.0`.

## [0.10.1] - 2026-09-13

### Fixed

- **Search initialization** -- Retry transient search dependency failures, clean up partially initialized services, and preserve shutdown ownership.
- **Shell pipeline classification** -- Require a complete shell token after a download pipe so hash utilities such as `sha256sum` are not misclassified as direct shell execution.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.10.1`.

## [0.10.0] - 2026-09-07

### Added

- **Minimum evaluation outcome** -- Let clients require an `approve`, `escalate`, or `deny` outcome for an evaluation and retry an approved operation with its audit call ID.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.10.0`.

## [0.9.2] - 2026-09-06

### Fixed

- **Path policy context** -- Evaluate write operations against the working directory for the individual tool call and preserve absolute target-path policy facts without an executor directory.
- **Curl credential redaction** -- Redact Basic Auth passwords passed through `curl --user` and `curl -u` before command data reaches evaluation and audit records.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.9.2`.

## [0.9.1] - 2026-08-30

### Fixed

- **Vector search startup** -- Restore the configured vector search provider during application startup.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.9.1`.

## [0.9.0] - 2026-08-16

### Added

- **Retained event history** -- Report the available history range for session events so clients can distinguish retained from unavailable records.
- **Claude Code installer** -- Add an installer and hardened hook integration for Intaris guardrail sessions.

### Fixed

- **Event retrieval** -- Reconcile retained event availability and cache behavior when event history changes.
- **Hook safety and concurrency** -- Close Claude Code guardrail bypass paths, preserve tool correlation, and prevent concurrent counter lost updates.
- **Database responsiveness** -- Offload blocking database work from the API execution path.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.9.0`.

## [0.8.0] - 2026-08-09

### Added

- **Runtime metrics** -- Expose Prometheus metrics, performance diagnostics, and a dedicated metrics listener for production observability.
- **Event-store cache** -- Cache immutable S3 event chunks and bound session read work to improve event retrieval performance.

### Changed

- **Intention reconciliation** -- Reconcile intention state from recorded events and update it atomically when eligibility changes.
- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.8.0`.

### Fixed

- **Optional service startup** -- Keep search and MCP initialization failures from blocking API startup.
- **Analysis credentials** -- Reuse the evaluate LLM API key for specialized analysis configurations when no dedicated key is set.

## [0.7.1] - 2026-08-02

### Fixed

- **Sensitive path reads** -- Route reads of sensitive project paths through guardrail evaluation, including shell, attached-file, and environment-variable forms.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.7.1`.

## [0.7.0] - 2026-07-19

### Added

- **Unified audit timeline** -- Project recorded tool calls and results into the audit API and management UI, with source-aware filtering and bounded event metadata.
- **Event pagination** -- Add before-sequence pagination for recorded session events, enabling efficient navigation of long event streams.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.7.0`.

## [0.6.1] - 2026-06-28

### Fixed

- **MCP evaluation payloads** -- Compact large MCP tool arguments before guardrail evaluation while preserving full data for execution, preventing oversized proxy requests from overloading Intaris evaluation.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.6.1`.

## [0.6.0] - 2026-06-07

### Added

- **Exact session event lookup** -- Add API and event-store support for retrieving a specific recorded session event by identifier.

### Fixed

- **Evaluate guardrails context** -- Propagate guardrails context consistently through evaluation and judge review paths so prompts and decisions see the expected contextual facts.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.6.0`.

## [0.5.0] - 2026-05-31

### Added

- **Session allow and deny policies** -- Add first-class session policy fields for allow and deny rules so guardrail behavior can be shaped per session.

### Fixed

- **Judge review context** -- Improve judge context assembly and non-interactive resolution handling for safer escalation decisions.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.5.0`.

## [0.4.9] - 2026-05-24

### Fixed

- **Health probes** -- Split cheap liveness and deeper readiness checks so container liveness no longer depends on database or analysis queue state.
- **Container healthcheck** -- Point the Docker image healthcheck at the lightweight `/live` endpoint.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.9`.

## [0.4.8] - 2026-05-16

### Fixed

- **Filesystem path policy** -- Extract patch target paths and expose working-directory policy facts to the evaluator so patch-style edits are judged against the correct project boundary.
- **Session policy updates** -- Persist allow, deny, and path policy changes when sessions are patched through the API.
- **Judge and evaluator policy context** -- Simplify policy context assembly so judge reviews and prompts see consistent working-directory policy details.
- **Configuration validation** -- Report malformed judge mode settings before missing LLM credentials so startup and tests fail on the actionable configuration error first.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.8`.

## [0.4.7] - 2026-05-04

### Added

- **Session deep links** -- Preserve and restore session detail selections from URL fragments so shared management UI links open directly to the intended session.

### Changed

- **Hermes plugin release** -- Release `hermes-intaris` `0.2.0` with audit call ID correlation, richer tool result recording, and aligned evaluation timeout handling.
- **OpenClaw plugin release** -- Release `@fpytloun/openclaw-intaris` `0.3.0` with audit call ID mapping, richer tool result recording, reusable escalation polling, and aligned evaluation timeout handling.
- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.7`.

## [0.4.6] - 2026-04-23

### Added

- **Assistant thinking events** -- Allow `assistant_thinking` session events so event ingestion, validation, and downstream consumers can capture internal assistant-thought stream records explicitly.

### Fixed

- **Intention updates** -- Prioritize active user pivots so fresh user direction wins over stale session framing during intention regeneration.
- **Structured output recovery** -- Recover from schema-invalid structured LLM responses instead of failing the request path when providers return malformed payloads.
- **PostgreSQL connection usage** -- Prevent PostgreSQL pool exhaustion under load by tightening database connection lifecycle handling.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.6`.

## [0.4.5] - 2026-04-19

### Added

- **Tool output viewer** -- Add on-demand tool output loading in the management UI so audit and session views can fetch and inspect stored outputs without preloading the full payload.
- **Cognis LLM-exposure audit events** -- Extend event ingestion, filtering, and session playback to capture Cognis message payload metadata for LLM-exposure auditing and turn reconstruction.

### Fixed

- **Cognis tool output matching** -- Match Cognis tool outputs by audit call ID so the UI consistently renders the correct result payload for each tool invocation.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.5`.

## [0.4.4] - 2026-04-15

### Fixed

- **Analysis scheduling** -- Keep L2/L3 queue state scoped by agent, preserve summary-attempt tracking, and prevent stale per-agent backlog from blocking the wrong analysis runs.
- **PostgreSQL startup** -- Defer the new analysis task scope index until after migrations so existing PostgreSQL deployments upgrade cleanly.
- **Server liveness** -- Isolate MCP session-manager restarts from the main app lifespan so background workers keep running through MCP transport failures.
- **MCP connectivity** -- Remove cross-server connect head-of-line blocking while preserving task-affine transport cleanup so MCP tool refreshes and `/api/v1/mcp/call` requests no longer stall behind unrelated upstream reconnects.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.4`.

## [0.4.3] - 2026-04-15

### Fixed

- **Claude Code context** -- Preserve reasoning context across turns so follow-up evaluations keep the right conversational state.
- **Analysis task tracking** -- Scope L2 and L3 task tracking by agent to avoid cross-agent interference in analysis scheduling.
- **PostgreSQL migrations** -- Avoid creating the pre-migration analysis task index on PostgreSQL before the required schema is in place.
- **Server shutdown isolation** -- Prevent MCP restart failures from cascading into background worker shutdown.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.3`.

## [0.4.2] - 2026-04-14

### Added

- **Claude Code hooks resilience** -- Improve hook execution resilience and add safer default path handling in the Claude Code integration.

### Fixed

- **Judge workflow** -- Persist explicit judge outcomes, refine advisory resolution behavior, and block evaluate responses until judge review reaches a final outcome.
- **LLM provider errors** -- Normalize transient upstream provider failures so retryable model errors are handled more consistently.
- **Prompt guidance** -- Keep authoritative human-approval precedent explicit in the evaluator decision rules.
- **Integration lifecycle** -- Align evaluate lifecycle handling across integrations for more consistent guardrail behavior.

### Changed

- **Claude Code scripts** -- Rename integration scripts and preserve executable bits in the packaged hook assets.
- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.2`.

## [0.4.1] - 2026-04-11

### Added

- **Human approval precedent** -- Feed final human approval decisions back into intention and evaluation prompts so follow-up calls on the same task inherit the user's explicit guidance more reliably.

### Fixed

- **Session metadata** -- Persist and expose `status_reason` and `agent_id` consistently in session APIs and bootstrap responses.
- **Background workers** -- Reuse analysis LLM clients and tighten task liveness tracking to reduce RSS growth and make stuck-task recovery more reliable.
- **Audit and evaluation** -- Honor final human same-tool approvals and share those precedents across similar low-risk tools to cut redundant escalations.
- **Event and storage IDs** -- Allow plus-addressed user IDs in the event store and encode path-based identifiers safely.
- **UI** -- Fix event pagination and Cognis tool name rendering in the management UI.

### Changed

- **Release metadata** -- Bump the Intaris package and MCP proxy client identity to `0.4.1`.

## [0.4.0] - 2026-04-02

### Added

- **Hermes Agent integration** -- Full `hermes-intaris` Python plugin with tool registry wrapping, session management, escalation polling, MCP tool proxy, session recording, and reasoning context forwarding. Published via PyPI with `hermes_agent.plugins` entry point. Includes CI workflow for automated publishing on tag.
- **Cross-service SSO** -- Exchange token endpoint (`POST /api/v1/auth/exchange`) for Cognis-to-Intaris single sign-on. Cookie-based session auth with `cognis_session` cookie support.
- **Session titles** -- Auto-generated session titles alongside intention updates for better session identification in the UI.
- **Event store reasoning resolution** -- `from_events` flag on `/reasoning` endpoint to resolve user message content from the event store, avoiding duplicate content when session recording is enabled.
- **Advisory mode defer-preferring** -- Judge advisory mode now defers borderline cases to human review instead of denying, only auto-denying unambiguously dangerous calls.

### Fixed

- **Decision matrix** -- Low/medium risk LLM denials are now escalated instead of denied, matching the documented decision matrix behavior.
- **PostgreSQL migration** -- Add missing `title` column migration for PostgreSQL deployments.
- **Auth hardening** -- Fix review findings in exchange token SSO flow.

### Changed

- **OpenCode plugin** -- When session recording is enabled, user messages are recorded to the event store first and `/reasoning` is called with `from_events=true` to avoid duplicate content.
- **OpenClaw plugin** -- Same `from_events` recording flow as OpenCode for consistency.

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

[0.4.8]: https://github.com/fpytloun/intaris/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/fpytloun/intaris/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/fpytloun/intaris/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/fpytloun/intaris/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/fpytloun/intaris/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/fpytloun/intaris/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/fpytloun/intaris/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/fpytloun/intaris/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/fpytloun/intaris/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/fpytloun/intaris/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/fpytloun/intaris/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/fpytloun/intaris/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/fpytloun/intaris/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fpytloun/intaris/releases/tag/v0.1.0
