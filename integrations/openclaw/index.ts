/**
 * OpenClaw Intaris Guardrails Plugin
 *
 * Intercepts every tool call and evaluates it through Intaris's safety
 * pipeline before allowing execution. Tool calls that are denied or
 * escalated are blocked with an error message.
 *
 * Flow:
 * 1. session_start: Creates an Intaris session via POST /api/v1/intention
 * 1a. subagent_spawning: Links child Intaris session to parent (pre-spawn)
 * 1b. before_reset: Closes the Intaris session when user sends /new or /reset
 * 2. before_agent_start: Forwards user prompt as reasoning context
 * 3. before_tool_call: Evaluates every tool call via POST /api/v1/evaluate
 *    - approve: tool executes normally
 *    - deny: returns { block: true, blockReason } (blocks execution)
 *    - escalate: polls for user decision, blocks until resolved
 * 4. after_tool_call: Records tool results for audit trail
 * 5. agent_end: Sends periodic checkpoints with session statistics
 * 6. session_end: Signals session completion to Intaris
 * 6a. subagent_ended: Completes child Intaris session when sub-agent ends
 *
 * Configuration via plugin config or environment variables:
 *   url / INTARIS_URL                          - Intaris server URL (default: http://localhost:8060)
 *   apiKey / INTARIS_API_KEY                    - API key for authentication
 *   (agentId is sourced from OpenClaw's hook context, not configurable)
 *   userId / INTARIS_USER_ID                    - User ID (optional if API key maps to user)
 *   failOpen / INTARIS_FAIL_OPEN                - Allow tool calls if Intaris is unreachable (default: false)
 *   allowPaths / INTARIS_ALLOW_PATHS            - Comma-separated parent directories for policy allow_paths
 *   escalationTimeout / INTARIS_ESCALATION_TIMEOUT - Max seconds to wait for escalation (default: 0 = no timeout)
 *   checkpointInterval / INTARIS_CHECKPOINT_INTERVAL - Evaluate calls between checkpoints (default: 25, 0 = disabled)
 *   recording / INTARIS_SESSION_RECORDING       - Enable session recording (default: false)
 *   recordToolOutput / INTARIS_RECORD_TOOL_OUTPUT - Record full tool output in events (default: follows recording)
 *   recordingFlushSize / INTARIS_RECORDING_FLUSH_SIZE - Events per recording batch (default: 50)
 *   recordingFlushMs / INTARIS_RECORDING_FLUSH_MS     - Recording flush interval in ms (default: 10000)
 *   mcpTools / INTARIS_MCP_TOOLS                      - Enable MCP tool proxy (default: true)
 *   mcpToolsCacheTtlMs / INTARIS_MCP_TOOLS_CACHE_TTL_MS - MCP tool list cache TTL in ms (default: 900000 = 15 min)
 */

import { Type } from "@sinclair/typebox";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk/core";
import { IntarisClient } from "./client.js";
import type {
  EvaluateResponse,
  IntarisConfig,
  McpCallResult,
  McpToolCache,
  McpToolDef,
  RecordingEvent,
  SessionState,
} from "./types.js";

// ============================================================================
// Constants
// ============================================================================

const MAX_SESSIONS = 100;
const MAX_RECENT_TOOLS = 10;

// ============================================================================
// Config Resolution
// ============================================================================

function resolveConfig(pluginConfig?: Record<string, unknown>): IntarisConfig {
  const cfg = pluginConfig || {};

  const rawEscalationTimeout = Number(
    cfg.escalationTimeout ?? process.env.INTARIS_ESCALATION_TIMEOUT ?? 0,
  );
  const rawCheckpointInterval = Number(
    cfg.checkpointInterval ?? process.env.INTARIS_CHECKPOINT_INTERVAL ?? 25,
  );
  const rawRecordingFlushSize = Number(
    cfg.recordingFlushSize ?? process.env.INTARIS_RECORDING_FLUSH_SIZE ?? 50,
  );
  const rawRecordingFlushMs = Number(
    cfg.recordingFlushMs ?? process.env.INTARIS_RECORDING_FLUSH_MS ?? 10000,
  );
  const rawMcpToolsCacheTtlMs = Number(
    cfg.mcpToolsCacheTtlMs ?? process.env.INTARIS_MCP_TOOLS_CACHE_TTL_MS ?? 900000,
  );

  return {
    url: String(cfg.url || process.env.INTARIS_URL || "http://localhost:8060"),
    apiKey: String(cfg.apiKey || process.env.INTARIS_API_KEY || ""),
    userId: String(cfg.userId || process.env.INTARIS_USER_ID || ""),
    failOpen:
      cfg.failOpen === true || (process.env.INTARIS_FAIL_OPEN || "false").toLowerCase() === "true",
    allowPaths: String(cfg.allowPaths || process.env.INTARIS_ALLOW_PATHS || ""),
    escalationTimeoutMs: isNaN(rawEscalationTimeout) ? 0 : Math.max(0, rawEscalationTimeout * 1000),
    checkpointInterval: isNaN(rawCheckpointInterval) ? 25 : rawCheckpointInterval,
    recording:
      cfg.recording === true ||
      (process.env.INTARIS_SESSION_RECORDING || "false").toLowerCase() === "true",
    recordToolOutput:
      cfg.recordToolOutput === false
        ? false
        : cfg.recordToolOutput === true ||
          (
            process.env.INTARIS_RECORD_TOOL_OUTPUT ||
            // Default: follows recording setting
            String(
              cfg.recording === true ||
                (process.env.INTARIS_SESSION_RECORDING || "false").toLowerCase() === "true",
            )
          ).toLowerCase() === "true",
    recordingFlushSize: isNaN(rawRecordingFlushSize) ? 50 : rawRecordingFlushSize,
    recordingFlushMs: isNaN(rawRecordingFlushMs) ? 10000 : rawRecordingFlushMs,
    mcpTools:
      cfg.mcpTools === false
        ? false
        : cfg.mcpTools === true ||
          (process.env.INTARIS_MCP_TOOLS || "true").toLowerCase() === "true",
    mcpToolsCacheTtlMs: isNaN(rawMcpToolsCacheTtlMs) ? 900000 : rawMcpToolsCacheTtlMs,
  };
}

// ============================================================================
// Helpers
// ============================================================================

function createSessionState(): SessionState {
  return {
    intarisSessionId: null,
    sessionCreated: false,
    callCount: 0,
    approvedCount: 0,
    deniedCount: 0,
    escalatedCount: 0,
    recentTools: [],
    lastError: null,
    intentionPending: false,
    intentionUpdated: false,
    isIdle: false,
    recordingBuffer: [],
    lastAssistantText: "",
    toolCallAuditIds: new Map(),
  };
}

/**
 * Build session policy from allowPaths config.
 * Expands ~ to home directory and converts each path to a glob pattern.
 * Always includes common temp directories so agent temp file operations
 * don't trigger out-of-project LLM evaluation.
 */
function buildPolicy(allowPaths: string): Record<string, unknown> {
  // Default: always allow common temp directories.
  const defaultPaths = ["/tmp/*", "/var/tmp/*"];
  // macOS uses $TMPDIR (e.g. /var/folders/xx/yyy/T/) instead of /tmp.
  const tmpDir = process.env.TMPDIR;
  if (tmpDir) {
    defaultPaths.push(`${tmpDir.replace(/\/+$/, "")}/*`);
  }

  const home = process.env.HOME || process.env.USERPROFILE || "";
  const userPaths = allowPaths
    ? allowPaths
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean)
        .map((p) => {
          if (p.startsWith("~/") || p === "~") {
            p = home + p.slice(1);
          }
          if (!p.endsWith("*")) {
            p = p.endsWith("/") ? p + "*" : p + "/*";
          }
          return p;
        })
    : [];

  return { allow_paths: [...defaultPaths, ...userPaths] };
}

// ============================================================================
// Module-level shared state
// ============================================================================

// The plugin may be loaded multiple times (gateway startup vs agent run)
// with different workspaceDir values, causing separate register() calls.
// All mutable state lives at module level so both instances share it.

const sessions = new Map<string, SessionState>();

// MCP tool cache — shared across instances so the tool factory (which may
// be registered by either instance) always reads the same cache.
let mcpToolCache: McpToolCache | null = null;
let mcpToolFetchInFlight: Promise<McpToolDef[]> | null = null;

function getOrCreateState(sessionKey: string): SessionState {
  let state = sessions.get(sessionKey);
  if (!state) {
    state = createSessionState();
    sessions.set(sessionKey, state);
    // Evict oldest entries if over limit
    if (sessions.size > MAX_SESSIONS) {
      const excess = sessions.size - MAX_SESSIONS;
      let count = 0;
      for (const key of sessions.keys()) {
        if (count >= excess) break;
        sessions.delete(key);
        count++;
      }
    }
  }
  return state;
}

// ============================================================================
// Plugin Definition
// ============================================================================

const intarisPlugin = {
  id: "intaris",
  name: "Intaris Guardrails",
  description: "Tool call safety evaluation via Intaris",
  configSchema: emptyPluginConfigSchema(),

  register(api: OpenClawPluginApi) {
    const cfg = resolveConfig(api.pluginConfig);
    const log = (
      level: "info" | "warn" | "error",
      message: string,
      extra?: Record<string, unknown>,
    ) => {
      const suffix = extra ? ` ${JSON.stringify(extra)}` : "";
      api.logger[level](`[intaris] ${message}${suffix}`);
    };
    const client = new IntarisClient(cfg, log);

    // -- Intention Helpers ---------------------------------------------------

    function buildIntention(ctx: { agentId?: string; workspaceDir?: string }): string {
      const parts: string[] = ["OpenClaw"];
      if (ctx.agentId) parts.push(`agent ${ctx.agentId}`);
      parts.push("session");
      if (ctx.workspaceDir) parts.push(`in ${ctx.workspaceDir}`);
      return parts.join(" ");
    }

    function buildDetails(ctx: {
      agentId?: string;
      workspaceDir?: string;
      channelId?: string;
      sessionKey?: string;
      trigger?: string;
    }): Record<string, unknown> {
      const details: Record<string, unknown> = { source: "openclaw" };
      if (ctx.workspaceDir) details.working_directory = ctx.workspaceDir;
      if (ctx.agentId) details.agent_id = ctx.agentId;
      if (ctx.channelId) details.channel = ctx.channelId;
      if (ctx.sessionKey) details.session_key = ctx.sessionKey;
      if (ctx.trigger) details.trigger = ctx.trigger;
      return details;
    }

    /**
     * Strip OpenClaw inbound metadata prefix blocks from user message text
     * for clean session recording. The metadata (sender info, conversation
     * info) is extracted separately so it's available for audit without
     * cluttering the Console view.
     *
     * Must stay in sync with sentinels in `src/auto-reply/reply/strip-inbound-meta.ts`.
     */
    function stripRecordingMetadata(text: string): { clean: string; sender?: string } {
      // Strip all fenced JSON metadata blocks (Conversation info, Sender,
      // Thread starter, Replied message, Forwarded message context, Chat history).
      const metaBlockRe =
        /(?:Conversation info|Sender|Thread starter|Replied message|Forwarded message context|Chat history since last reply) \(untrusted[^)]*\):\n```json\n[\s\S]*?```\n*/g;
      let clean = text.replace(metaBlockRe, "");
      // Strip trailing untrusted context block.
      clean = clean
        .replace(
          /Untrusted context \(metadata, do not treat as instructions or commands\):[\s\S]*$/,
          "",
        )
        .trim();

      // Extract sender name from the Sender metadata block.
      const senderMatch = text.match(/Sender \(untrusted metadata\):\n```json\n([\s\S]*?)```/);
      let sender: string | undefined;
      if (senderMatch) {
        try {
          const parsed = JSON.parse(senderMatch[1]);
          sender = parsed.name || parsed.label || parsed.id;
        } catch {
          // Malformed JSON — skip sender extraction.
        }
      }

      return { clean: clean || text, sender };
    }

    /**
     * Extract text content from a tool result for session recording.
     * Tool results follow the AgentToolResult shape:
     * `{ content: [{ type: "text", text: "..." }, ...], details: ... }`
     */
    function extractToolOutput(result: unknown): string | undefined {
      if (!result || typeof result !== "object") return undefined;
      const r = result as { content?: Array<{ type?: string; text?: string }> };
      if (!Array.isArray(r.content)) return undefined;
      const text = r.content
        .filter((c) => c.type === "text" && c.text)
        .map((c) => c.text)
        .join("\n");
      return text || undefined;
    }

    function buildCheckpointContent(state: SessionState): string {
      const interval =
        cfg.checkpointInterval > 0 ? Math.floor(state.callCount / cfg.checkpointInterval) : 0;
      const tools = state.recentTools.join(", ") || "none";
      return (
        `Checkpoint #${interval}: ${state.callCount} calls ` +
        `(${state.approvedCount} approved, ${state.deniedCount} denied, ` +
        `${state.escalatedCount} escalated). Recent tools: ${tools}`
      );
    }

    function buildAgentSummary(
      state: SessionState,
      ctx: { agentId?: string; workspaceDir?: string },
    ): string {
      const agent = ctx.agentId ? ` (${ctx.agentId})` : "";
      return (
        `OpenClaw session${agent} completed. ${state.callCount} tool calls ` +
        `(${state.approvedCount} approved, ${state.deniedCount} denied, ` +
        `${state.escalatedCount} escalated). ` +
        `Working directory: ${ctx.workspaceDir || "unknown"}`
      );
    }

    // -- Session Management --------------------------------------------------

    /**
     * Ensure an Intaris session exists for the given OpenClaw session.
     * Creates one via POST /api/v1/intention if needed.
     * Returns the Intaris session_id, or null on failure.
     */
    async function ensureSession(
      sessionKey: string,
      state: SessionState,
      ctx: {
        agentId?: string;
        workspaceDir?: string;
        channelId?: string;
        sessionKey?: string;
        trigger?: string;
      },
    ): Promise<string | null> {
      if (state.intarisSessionId) return state.intarisSessionId;

      // If another call is already creating the session, wait for it
      // instead of creating a duplicate. This prevents the race between
      // session_start (fire-and-forget) and before_agent_start (awaited).
      if (state.creating) return state.creating;

      state.creating = (async () => {
        // Unique Intaris session ID per session instance. Each gateway restart
        // or /new / /reset gets a fresh session — no resume-on-reconnect.
        const intarisSessionId = `oc-${crypto.randomUUID()}`;
        const intention = buildIntention(ctx);
        const details: Record<string, unknown> = buildDetails(ctx);
        const policy = buildPolicy(cfg.allowPaths);

        // Enrich details with sub-agent metadata when available.
        if (state.parentIntarisSessionId) {
          details.subagent = true;
          if (state.subagentLabel) details.subagent_label = state.subagentLabel;
          if (state.subagentMode) details.subagent_mode = state.subagentMode;
          // Compute depth from session key (count ":subagent:" segments).
          const depth = (ctx.sessionKey || "").split(":subagent:").length - 1;
          if (depth > 0) details.subagent_depth = depth;
        }

        const { data, error, status } = await client.createIntention(
          intarisSessionId,
          intention,
          details,
          policy,
          state.parentIntarisSessionId || null,
          ctx.agentId,
        );

        if (data) {
          state.intarisSessionId = intarisSessionId;
          state.sessionCreated = true;
          log("info", `Session created: ${intarisSessionId}`);
        } else if (status === 409) {
          // Session already exists (resumed session) -- reuse it
          state.intarisSessionId = intarisSessionId;
          log("info", `Session already exists, reusing: ${intarisSessionId}`);
          // Re-activate and update intention
          client.updateStatus(intarisSessionId, "active", ctx.agentId).catch(() => {});
          client.updateSession(intarisSessionId, intention, details, ctx.agentId).catch(() => {});
        } else if (status !== null && status >= 400 && status < 500) {
          // Client error (auth, validation) -- propagate, don't retry
          state.lastError = error || `HTTP ${status}`;
          return null;
        } else {
          // Server error or network issue -- try using it anyway
          state.intarisSessionId = intarisSessionId;
        }

        return state.intarisSessionId;
      })();

      try {
        return await state.creating;
      } finally {
        state.creating = undefined;
      }
    }

    // -- Recording Helpers ---------------------------------------------------

    function recordEvent(sessionKey: string, event: RecordingEvent): void {
      if (!cfg.recording) return;
      const state = sessions.get(sessionKey);
      if (!state?.intarisSessionId) return;

      state.recordingBuffer.push(event);

      // Auto-flush when buffer reaches threshold
      if (state.recordingBuffer.length >= cfg.recordingFlushSize) {
        flushRecordingBuffer(sessionKey);
      }
    }

    function flushRecordingBuffer(sessionKey: string, agentId?: string): void {
      const state = sessions.get(sessionKey);
      if (!state?.intarisSessionId) return;
      if (state.recordingBuffer.length === 0) return;

      const events = state.recordingBuffer.splice(0);
      client.appendEvents(state.intarisSessionId, events, agentId).catch((err) => {
        log("warn", `Recording flush failed for ${state.intarisSessionId}: ${err}`, {
          eventCount: events.length,
        });
      });
    }

    // Periodic recording flush timer
    let recordingFlushTimer: ReturnType<typeof setInterval> | null = null;
    if (cfg.recording) {
      recordingFlushTimer = setInterval(() => {
        for (const [sessionKey] of sessions) {
          flushRecordingBuffer(sessionKey);
        }
      }, cfg.recordingFlushMs);
    }

    // -- Completion Helpers --------------------------------------------------

    function signalCompletion(
      state: SessionState,
      sessionKey: string,
      ctx: { agentId?: string; workspaceDir?: string },
    ): void {
      if (!state.intarisSessionId) return;

      // Flush recording buffer before completion
      flushRecordingBuffer(sessionKey, ctx.agentId);

      const intarisId = state.intarisSessionId;
      // Fire both calls in parallel -- neither blocks the other
      Promise.all([
        client.updateStatus(intarisId, "completed", ctx.agentId),
        client.submitAgentSummary(intarisId, buildAgentSummary(state, ctx), ctx.agentId),
      ]).catch(() => {});
    }

    function sendCheckpoint(state: SessionState, agentId?: string): void {
      if (cfg.checkpointInterval <= 0) return;
      if (state.callCount % cfg.checkpointInterval !== 0) return;
      if (!state.intarisSessionId) return;

      client
        .submitCheckpoint(state.intarisSessionId, buildCheckpointContent(state), agentId)
        .catch(() => {});
    }

    // -- MCP Tool Proxy -------------------------------------------------------

    /** Refresh the MCP tool cache from the Intaris backend. */
    async function refreshMcpToolCache(agentId?: string): Promise<McpToolDef[]> {
      // Deduplicate concurrent fetches
      if (mcpToolFetchInFlight) return mcpToolFetchInFlight;

      mcpToolFetchInFlight = (async () => {
        try {
          const tools = await client.listMcpTools(agentId);
          mcpToolCache = { tools, fetchedAt: Date.now() };
          if (tools.length > 0) {
            log(
              "info",
              `MCP tool cache refreshed: ${tools.length} tools from ${new Set(tools.map((t) => t.server)).size} server(s)`,
            );
          }
          return tools;
        } catch (err) {
          log("warn", `MCP tool cache refresh failed: ${err}`);
          return mcpToolCache?.tools ?? [];
        } finally {
          mcpToolFetchInFlight = null;
        }
      })();

      return mcpToolFetchInFlight;
    }

    /** Check if the MCP tool cache is stale. */
    function isMcpCacheStale(): boolean {
      if (!mcpToolCache) return true;
      return Date.now() - mcpToolCache.fetchedAt > cfg.mcpToolsCacheTtlMs;
    }

    /**
     * Build an OpenClaw AgentTool from an MCP tool definition.
     * The execute function proxies the call through Intaris.
     */
    function buildMcpAgentTool(mcpTool: McpToolDef) {
      const toolName = `${mcpTool.server}_${mcpTool.name}`;
      return {
        name: toolName,
        label: mcpTool.title || mcpTool.name,
        description: `[MCP: ${mcpTool.server}] ${mcpTool.description || mcpTool.name}`,
        parameters: Type.Unsafe(mcpTool.inputSchema),
        async execute(_toolCallId: string, params: Record<string, unknown>) {
          // Find the Intaris session for the current context.
          // The tool factory doesn't have direct access to sessionKey, so we
          // look up the first active (non-idle) session, falling back to any session.
          let intarisSessionId: string | null = null;
          for (const [, state] of sessions) {
            if (state.intarisSessionId) {
              intarisSessionId = state.intarisSessionId;
              if (!state.isIdle) break;
            }
          }

          if (!intarisSessionId) {
            return {
              content: [{ type: "text", text: `[intaris] No active session for MCP tool call` }],
              details: { error: "no_session" },
            };
          }

          const { data, error } = await client.callMcpTool(
            intarisSessionId,
            mcpTool.server,
            mcpTool.name,
            params,
          );

          if (!data) {
            return {
              content: [
                {
                  type: "text",
                  text: `[intaris] MCP tool call failed: ${error || "unknown error"}`,
                },
              ],
              details: { error: error || "unknown" },
            };
          }

          const result = data as McpCallResult;

          // Handle safety evaluation decisions from Intaris.
          // Deny and escalate are returned as structured responses so the
          // agent sees the decision and can react appropriately.
          if (result.decision === "deny") {
            const reason = result.reasoning || "Tool call denied by safety evaluation";
            log("info", `MCP ${mcpTool.server}:${mcpTool.name}: DENIED (${result.call_id})`);
            return {
              content: [{ type: "text", text: `[intaris] DENIED: ${reason}` }],
              details: { decision: "deny", call_id: result.call_id, latency_ms: result.latency_ms },
            };
          }

          if (result.decision === "escalate") {
            const reason = result.reasoning || "Tool call requires human approval";
            log(
              "warn",
              `MCP ${mcpTool.server}:${mcpTool.name}: ESCALATED (${result.call_id}): ${reason}`,
            );
            return {
              content: [
                {
                  type: "text",
                  text:
                    `[intaris] ESCALATED (${result.call_id}): ${reason}\n` +
                    `Approve or deny in the Intaris UI, then retry this tool call.`,
                },
              ],
              details: {
                decision: "escalate",
                call_id: result.call_id,
                latency_ms: result.latency_ms,
              },
            };
          }

          if (result.isError) {
            const errorText =
              result.content?.map((c) => c.text || JSON.stringify(c)).join("\n") ||
              "MCP tool returned an error";
            return {
              content: [{ type: "text", text: `[MCP error] ${errorText}` }],
              details: { isError: true, latency_ms: result.latency_ms },
            };
          }

          // Map MCP content to OpenClaw content format
          const content = (result.content || []).map((c) => ({
            type: "text" as const,
            text: c.text || JSON.stringify(c),
          }));

          return {
            content: content.length > 0 ? content : [{ type: "text", text: "(empty result)" }],
            details: { latency_ms: result.latency_ms },
          };
        },
      };
    }

    // Register MCP tool factory if enabled.
    // The factory is called synchronously per agent run, so it reads from the cache.
    if (cfg.mcpTools) {
      // Eagerly fetch the tool list at plugin init (best-effort, non-blocking).
      refreshMcpToolCache().catch(() => {});

      // The factory return type is structurally compatible with AnyAgentTool[]
      // but TypeScript widens `type: "text"` to `string` in mapped arrays.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      api.registerTool(((ctx: { agentId?: string }) => {
        // Return cached MCP tools as OpenClaw AgentTool objects.
        // If cache is stale, trigger a background refresh for the next run.
        if (isMcpCacheStale()) {
          refreshMcpToolCache(ctx.agentId).catch(() => {});
        }

        const tools = mcpToolCache?.tools;
        if (!tools || tools.length === 0) return null;

        return tools.map(buildMcpAgentTool);
      }) as Parameters<typeof api.registerTool>[0]);
    }

    // -- Hooks ---------------------------------------------------------------

    // Initialization logging
    if (!cfg.apiKey) {
      log(
        "warn",
        "API key not configured -- plugin will fail to authenticate. Set INTARIS_API_KEY or plugins.intaris.apiKey.",
      );
    }
    if (cfg.failOpen) {
      log(
        "warn",
        "Fail-open mode enabled -- tool calls will proceed unchecked if Intaris is unreachable.",
      );
    }
    log("info", "Plugin initialized", {
      url: cfg.url,
      failOpen: cfg.failOpen,
      checkpointInterval: cfg.checkpointInterval,
      recording: cfg.recording,
      mcpTools: cfg.mcpTools,
    });

    // -- Hooks ---------------------------------------------------------------
    // Hooks are registered on every register() call because each call creates
    // a new PluginRegistry and the global hook runner is replaced. Side effects
    // are deduplicated at execution time via the shared module-level sessions
    // Map (e.g., ensureSession dedup promise, per-turn reasoning dedup).

    // -- session_start: Create Intaris session --------------------------------
    api.on("session_start", async (_event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = getOrCreateState(sessionKey);
      // Pre-create the Intaris session (best-effort, non-blocking)
      ensureSession(sessionKey, state, ctx).catch(() => {});

      // Refresh MCP tool cache on session start (best-effort, non-blocking)
      if (cfg.mcpTools && isMcpCacheStale()) {
        refreshMcpToolCache(ctx.agentId).catch(() => {});
      }
    });

    // -- subagent_spawning: Link child Intaris session to parent ----------------
    // Fires BEFORE the child session starts, so we can pre-populate the
    // child's state with the parent Intaris session ID. When session_start
    // fires for the child and calls ensureSession, it will pass the
    // parent_session_id to the Intaris backend.
    api.on("subagent_spawning", async (event, ctx) => {
      const childKey = event.childSessionKey;
      if (!childKey) return { status: "ok" as const };

      const requesterKey = ctx.requesterSessionKey;
      const parentState = requesterKey ? sessions.get(requesterKey) : undefined;

      const childState = getOrCreateState(childKey);
      if (parentState?.intarisSessionId) {
        childState.parentIntarisSessionId = parentState.intarisSessionId;
        log("info", `Sub-agent ${childKey}: linked to parent ${parentState.intarisSessionId}`);
      }
      // Store sub-agent metadata for session details enrichment.
      if (event.label) childState.subagentLabel = event.label;
      if (event.mode) childState.subagentMode = event.mode;

      return { status: "ok" as const };
    });

    // -- subagent_ended: Complete child Intaris session -------------------------
    api.on("subagent_ended", async (event) => {
      const sessionKey = event.targetSessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state?.intarisSessionId) return;

      log("info", `Sub-agent ${sessionKey}: ended (${event.outcome || event.reason})`);
      signalCompletion(state, sessionKey, {});
      sessions.delete(sessionKey);
    });

    // -- before_reset: User sent /new or /reset — close current Intaris session
    api.on("before_reset", async (_event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state) return;

      // Signal completion to Intaris and wipe local state so the
      // subsequent session_start creates a fresh Intaris session.
      signalCompletion(state, sessionKey, ctx);
      sessions.delete(sessionKey);
    });

    // -- before_agent_start: Forward user prompt as reasoning context ----------
    // Uses before_agent_start instead of message_received because the latter
    // does not expose sessionKey in its context (PluginHookMessageContext only
    // has channelId/accountId/conversationId).
    api.on("before_agent_start", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state?.intarisSessionId) {
        // Ensure session exists (may not have been created yet if session_start
        // fired before config was ready)
        const s = getOrCreateState(sessionKey);
        await ensureSession(sessionKey, s, ctx).catch(() => {});
        if (!s.intarisSessionId) return;
      }

      const stateRef = sessions.get(sessionKey);
      if (!stateRef) return;

      const content = typeof event.prompt === "string" ? event.prompt.trim() : "";
      if (!content) return;

      // Resume session from idle when user provides new input
      if (stateRef.isIdle) {
        stateRef.isIdle = false;
        client.updateStatus(stateRef.intarisSessionId!, "active", ctx.agentId).catch(() => {});
      }

      // Consume last assistant text as context for intention generation.
      // This helps the intention generator interpret short user replies like
      // "ok, do it" by providing the assistant's last response as context.
      const assistantContext = stateRef.lastAssistantText || undefined;
      stateRef.lastAssistantText = "";

      // Strip inbound metadata (sender/conversation info) from the user
      // message so it doesn't pollute reasoning context or session recording.
      const { clean: cleanUserText, sender: userSender } = stripRecordingMetadata(content);

      // Dedup: the plugin may be registered on multiple PluginRegistry instances
      // (gateway startup + agent run). Both fire before_agent_start for the same
      // prompt. Skip if this exact prompt was already submitted this turn.
      if (stateRef.lastReasoningPrompt === cleanUserText) return;
      stateRef.lastReasoningPrompt = cleanUserText;

      if (cfg.recording) {
        // When session recording is enabled, record the user message
        // event first, flush synchronously to ensure it's in the event
        // store, then trigger reasoning with from_events=true to avoid
        // re-sending the content that's already been recorded.
        // The flush must complete before the reasoning call so Intaris
        // can resolve the user message from the event store.
        recordEvent(sessionKey, {
          type: "message",
          data: {
            role: "user",
            text: cleanUserText,
            sender: userSender,
            sessionKey,
          },
        });

        // Await the flush directly instead of using the fire-and-forget
        // flushRecordingBuffer helper — the /reasoning call depends on
        // the events being persisted server-side.
        const flushState = sessions.get(sessionKey);
        if (flushState?.intarisSessionId && flushState.recordingBuffer.length > 0) {
          const events = flushState.recordingBuffer.splice(0);
          await client.appendEvents(flushState.intarisSessionId, events, ctx.agentId).catch(() => {});
        }

        client
          .submitReasoning(
            stateRef.intarisSessionId!,
            "",
            ctx.agentId,
            undefined,
            true,
          )
          .catch(() => {});
      } else {
        // Without recording, send the content directly via /reasoning.
        client
          .submitReasoning(
            stateRef.intarisSessionId!,
            `User message: ${cleanUserText}`,
            ctx.agentId,
            assistantContext,
          )
          .catch(() => {});

        // Record clean user message for session recording (no-op when
        // recording is disabled since recordEvent checks cfg.recording).
        recordEvent(sessionKey, {
          type: "message",
          data: {
            role: "user",
            text: cleanUserText,
            sender: userSender,
            sessionKey,
          },
        });
      }

      // Signal that an intention update is in flight. The next
      // before_tool_call will include intention_pending=true so the
      // server waits for the /reasoning call to arrive before evaluating.
      stateRef.intentionPending = true;
    });

    // -- before_tool_call: Core guardrail ------------------------------------
    api.on("before_tool_call", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return {};

      const state = getOrCreateState(sessionKey);

      // Ensure session exists (lazy creation for resumed sessions)
      const intarisSessionId = await ensureSession(sessionKey, state, ctx);
      if (!intarisSessionId) {
        if (cfg.failOpen) return {};
        const detail = state.lastError || "unknown error";
        return { block: true, blockReason: `[intaris] Cannot create session: ${detail}` };
      }

      // Dedup: skip if this tool call was already evaluated by another plugin instance.
      if (event.toolCallId) {
        if (!state.evaluatedToolCalls) state.evaluatedToolCalls = new Set();
        if (state.evaluatedToolCalls.has(event.toolCallId)) return {};
        state.evaluatedToolCalls.add(event.toolCallId);
      }

      // Record tool call event (fire-and-forget)
      recordEvent(sessionKey, {
        type: "tool_call",
        data: {
          tool: event.toolName,
          args: event.params,
          call_id: event.toolCallId,
          toolCallId: event.toolCallId,
          sessionKey,
        },
      });

      // Skip evaluation for MCP tools — evaluation happens in POST /mcp/call
      // instead, avoiding double LLM evaluation. The MCP tool's execute()
      // function calls POST /mcp/call which runs the full safety pipeline.
      if (cfg.mcpTools && mcpToolCache?.tools) {
        const isMcpTool = mcpToolCache.tools.some(
          (t) => `${t.server}_${t.name}` === event.toolName,
        );
        if (isMcpTool) {
          return {};
        }
      }

      // Evaluate the tool call
      const intentionPending = state.intentionPending;
      const {
        data,
        error: evalError,
        status: evalStatus,
      } = await client.evaluate(
        intarisSessionId,
        event.toolName,
        event.params,
        intentionPending,
        ctx.agentId,
      );

      // Clear the flag after the first evaluate call
      if (intentionPending) {
        state.intentionPending = false;
      }

      if (!data) {
        // Distinguish config errors (4xx) from transient failures (5xx/network)
        if (evalStatus !== null && evalStatus >= 400 && evalStatus < 500) {
          return {
            block: true,
            blockReason: `[intaris] Evaluation rejected for ${event.toolName}: ${evalError}`,
          };
        }
        // Intaris unreachable or server error
        if (cfg.failOpen) {
          log("warn", `Evaluate failed for ${event.toolName} -- allowing (fail-open)`);
          return {};
        }
        return {
          block: true,
          blockReason: `[intaris] Evaluation failed for ${event.toolName}: ${evalError || "server unreachable"} (INTARIS_FAIL_OPEN=false)`,
        };
      }

      const result = data as unknown as EvaluateResponse;

      // Track decision statistics
      state.callCount++;
      if (event.toolCallId && result.call_id) {
        state.toolCallAuditIds?.set(event.toolCallId, result.call_id);
      }
      if (result.decision === "approve") state.approvedCount++;
      else if (result.decision === "deny") state.deniedCount++;
      else if (result.decision === "escalate") state.escalatedCount++;

      // Track recent tool names (bounded)
      state.recentTools = [...state.recentTools, event.toolName].slice(-MAX_RECENT_TOOLS);

      log(
        "info",
        `${event.toolName}: ${result.decision} (${result.path}, ${result.latency_ms}ms)`,
        {
          call_id: result.call_id,
          risk: result.risk,
        },
      );

      // Send periodic checkpoint (fire-and-forget)
      sendCheckpoint(state, ctx.agentId);

      // Try to update intention after gathering enough context
      if (!state.intentionUpdated && state.callCount >= 3) {
        state.intentionUpdated = true;
        client
          .updateSession(intarisSessionId, buildIntention(ctx), buildDetails(ctx), ctx.agentId)
          .catch(() => {});
      }

      const waitForEscalation = async (callId: string, reason: string) => {
        log(
          "warn",
          `ESCALATED ${event.toolName} (${callId}): ${reason}. Waiting for approval in Intaris UI...`,
        );

        const pollBackoffMs = [2000, 4000, 8000, 16000, 30000];
        const startTime = Date.now();
        let pollAttempt = 0;
        let lastReminderAt = startTime;

        while (true) {
          if (cfg.escalationTimeoutMs > 0 && Date.now() - startTime > cfg.escalationTimeoutMs) {
            return {
              block: true,
              blockReason:
                `[intaris] ESCALATION TIMEOUT (${callId}): ${reason}\n` +
                `No response within ${cfg.escalationTimeoutMs / 1000}s. Approve or deny in the Intaris UI.`,
            };
          }

          const now = Date.now();
          if (now - lastReminderAt >= 60000) {
            const waitSec = Math.round((now - startTime) / 1000);
            log(
              "warn",
              `Still waiting for escalation approval for ${event.toolName} (${callId})... ${waitSec}s elapsed`,
            );
            lastReminderAt = now;
          }

          const delay = pollBackoffMs[Math.min(pollAttempt, pollBackoffMs.length - 1)];
          await new Promise((resolve) => setTimeout(resolve, delay));
          pollAttempt++;

          const { data: auditData } = await client.getAudit(callId, ctx.agentId);
          if (!auditData) continue;

          const auditRecord = auditData as unknown as {
            user_decision?: string;
            user_note?: string;
          };

          if (auditRecord.user_decision === "approve") {
            log("info", `Escalation approved: ${event.toolName} (${callId})`);
            return {};
          }

          if (auditRecord.user_decision === "deny") {
            const denyNote = auditRecord.user_note ? ` -- ${auditRecord.user_note}` : "";
            return {
              block: true,
              blockReason: `[intaris] DENIED by user (${callId}): ${reason}${denyNote}`,
            };
          }
        }
      };

      // -- Handle DENY -------------------------------------------------------
      if (result.decision === "deny") {
        // Session-level suspension: wait for user action
        if (result.session_status === "suspended") {
          const statusReason = result.status_reason || "Session suspended";
          log("warn", `Session suspended: ${statusReason}. Waiting for approval in Intaris UI...`);

          // Poll GET /session/{id} with exponential backoff
          const suspendBackoffMs = [2000, 4000, 8000, 16000, 30000];
          const suspendStart = Date.now();
          let suspendAttempt = 0;
          let suspendLastReminder = suspendStart;

          while (true) {
            // Check timeout
            if (
              cfg.escalationTimeoutMs > 0 &&
              Date.now() - suspendStart > cfg.escalationTimeoutMs
            ) {
              return {
                block: true,
                blockReason:
                  `[intaris] SESSION SUSPENSION TIMEOUT: ${statusReason}\n` +
                  `No response within ${cfg.escalationTimeoutMs / 1000}s. Reactivate or terminate in the Intaris UI.`,
              };
            }

            // Periodic reminder every 60s
            const suspendNow = Date.now();
            if (suspendNow - suspendLastReminder >= 60000) {
              const waitSec = Math.round((suspendNow - suspendStart) / 1000);
              log(
                "warn",
                `Still waiting for session approval... ${waitSec}s elapsed. Reason: ${statusReason}`,
              );
              suspendLastReminder = suspendNow;
            }

            // Wait with exponential backoff
            const suspendDelay =
              suspendBackoffMs[Math.min(suspendAttempt, suspendBackoffMs.length - 1)];
            await new Promise((resolve) => setTimeout(resolve, suspendDelay));
            suspendAttempt++;

            // Poll session status
            const { data: sessionData } = await client.getSession(intarisSessionId, ctx.agentId);
            if (!sessionData) continue; // Server unreachable -- keep polling

            const sessionResponse = sessionData as unknown as {
              status: string;
              status_reason?: string;
            };

            if (sessionResponse.status === "active") {
              // Session reactivated -- re-evaluate this tool call
              log("info", `Session reactivated -- re-evaluating ${event.toolName}`);

              const { data: reData } = await client.evaluate(
                intarisSessionId,
                event.toolName,
                event.params,
                false,
                ctx.agentId,
              );

              if (!reData) {
                if (cfg.failOpen) return {};
                return {
                  block: true,
                  blockReason: `[intaris] Re-evaluation failed for ${event.toolName} after session reactivation`,
                };
              }

              const reResult = reData as unknown as EvaluateResponse;
              if (reResult.decision === "deny") {
                return {
                  block: true,
                  blockReason: `[intaris] DENIED: ${reResult.reasoning || "Tool call denied after session reactivation"}`,
                };
              }
              if (event.toolCallId && reResult.call_id) {
                state.toolCallAuditIds?.set(event.toolCallId, reResult.call_id);
              }
              if (reResult.decision === "escalate") {
                return await waitForEscalation(
                  reResult.call_id,
                  reResult.reasoning || "Tool call requires human approval",
                );
              }
              // Approved -- let tool proceed
              return {};
            }

            if (sessionResponse.status === "terminated") {
              return {
                block: true,
                blockReason: `[intaris] Session terminated: ${sessionResponse.status_reason || "terminated by user"}`,
              };
            }
            // Still suspended -- continue polling
          }
        }

        // Session termination: hard kill
        if (result.session_status === "terminated") {
          return {
            block: true,
            blockReason: `[intaris] Session terminated: ${result.status_reason || "terminated by user"}`,
          };
        }

        // Regular deny
        const reason = result.reasoning || "Tool call denied by safety evaluation";
        return { block: true, blockReason: `[intaris] DENIED: ${reason}` };
      }

      // -- Handle ESCALATE ---------------------------------------------------
      if (result.decision === "escalate") {
        const reason = result.reasoning || "Tool call requires human approval";
        return await waitForEscalation(result.call_id, reason);
      }

      // decision === "approve" (or escalation approved) -- tool call proceeds
      return {};
    });

    // -- after_tool_call: Record tool results ---------------------------------
    api.on("after_tool_call", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      // Dedup: skip if this tool result was already recorded by another plugin instance.
      if (event.toolCallId) {
        const state = sessions.get(sessionKey);
        if (state) {
          if (!state.recordedToolResults) state.recordedToolResults = new Set();
          if (state.recordedToolResults.has(event.toolCallId)) return;
          state.recordedToolResults.add(event.toolCallId);
        }
      }

      const state = sessions.get(sessionKey);
      const auditCallId = event.toolCallId
        ? state?.toolCallAuditIds?.get(event.toolCallId) || event.toolCallId
        : undefined;
      if (event.toolCallId) {
        state?.toolCallAuditIds?.delete(event.toolCallId);
      }

      recordEvent(sessionKey, {
        type: "tool_result",
        data: {
          tool: event.toolName,
          audit_call_id: auditCallId,
          call_id: event.toolCallId,
          toolCallId: event.toolCallId,
          sessionKey,
          error: event.error,
          is_error: Boolean(event.error),
          isError: Boolean(event.error),
          durationMs: event.durationMs,
          ...(cfg.recordToolOutput && event.result
            ? { output: extractToolOutput(event.result) }
            : {}),
        },
      });
    });

    // -- llm_output: Capture last assistant text for intention context ---------
    api.on("llm_output", async (event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state) return;

      // Store the last assistant text so it can be sent as context with the
      // next reasoning call, helping the intention generator interpret short
      // user replies like "ok, do it".
      const texts = event.assistantTexts;
      if (texts && texts.length > 0) {
        const assistantText = texts[texts.length - 1];
        state.lastAssistantText = assistantText;

        // Dedup: skip if this exact assistant text was already recorded.
        if (state.lastRecordedAssistantText === assistantText) return;
        state.lastRecordedAssistantText = assistantText;

        // Record assistant message for session recording
        recordEvent(sessionKey, {
          type: "message",
          data: {
            role: "assistant",
            text: assistantText,
            sessionKey,
          },
        });
      }
    });

    // -- agent_end: Send checkpoint if interval reached -----------------------
    api.on("agent_end", async (_event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state?.intarisSessionId) return;

      // Transition to idle -- the agent run is done, waiting for next input
      if (!state.isIdle) {
        state.isIdle = true;
        client.updateStatus(state.intarisSessionId, "idle", ctx.agentId).catch(() => {});
      }
    });

    // -- session_end: Signal completion to Intaris ----------------------------
    api.on("session_end", async (_event, ctx) => {
      const sessionKey = ctx.sessionKey;
      if (!sessionKey) return;

      const state = sessions.get(sessionKey);
      if (!state) return;

      signalCompletion(state, sessionKey, ctx);
      sessions.delete(sessionKey);
    });

    // -- Cleanup: clear recording timer on gateway stop ----------------------
    api.on("gateway_stop", async () => {
      if (recordingFlushTimer) {
        clearInterval(recordingFlushTimer);
        recordingFlushTimer = null;
      }
      // Flush all remaining recording buffers
      for (const [sessionKey] of sessions) {
        flushRecordingBuffer(sessionKey);
      }
    });
  },
};

export default intarisPlugin;
