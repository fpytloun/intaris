/**
 * intaris — Guardrails plugin for OpenCode.
 *
 * This plugin intercepts every tool call and evaluates it through
 * Intaris's safety pipeline before allowing execution. Tool calls
 * that are denied or escalated are blocked with an error message.
 *
 * Flow:
 * 1. session.created: Creates an Intaris session via POST /api/v1/intention
 *    (including child sessions with parent_session_id)
 * 2. chat.message: Captures agent name per session for sub-agent identification
 * 3. tool.execute.before: Evaluates every tool call via POST /api/v1/evaluate
 *    - approve: tool executes normally
 *    - deny: throws error with reasoning (blocks execution)
 *    - escalate: throws error directing user to Intaris UI for approval
 *    - Periodic checkpoints sent every N calls (configurable)
 * 4. session.deleted / session.idle: Signals session completion to Intaris
 *    - PATCH /session/{id}/status to "completed"
 *    - POST /session/{id}/agent-summary with session statistics
 *
 * Configuration via environment variables:
 *   INTARIS_URL                  - Intaris server URL (default: http://localhost:8060)
 *   INTARIS_API_KEY              - API key for authentication (required)
 *   INTARIS_AGENT_ID             - Agent ID (default: opencode)
 *   INTARIS_USER_ID              - User ID (optional if API key maps to user)
 *   INTARIS_FAIL_OPEN            - Allow tool calls if Intaris is unreachable (default: false)
 *   INTARIS_INTENTION            - Session intention override (default: auto-generated)
 *   INTARIS_CHECKPOINT_INTERVAL  - Evaluate calls between checkpoints (default: 25, 0=disabled)
 */

import type { Plugin } from "@opencode-ai/plugin"

interface SessionState {
  intarisSessionId: string | null
  sessionCreated: boolean
  callCount: number
  approvedCount: number
  deniedCount: number
  escalatedCount: number
  recentTools: string[]
  parentSessionId: string | null
  lastError: string | null
  agentName: string | null
  sessionTitle: string | null
  intentionUpdated: boolean
}

interface EvaluateResponse {
  call_id: string
  decision: "approve" | "deny" | "escalate"
  reasoning?: string
  risk?: string
  path: string
  latency_ms: number
}

export const IntarisPlugin: Plugin = async ({ client, worktree, directory }) => {
  // -- Configuration --------------------------------------------------------
  const baseUrl = process.env.INTARIS_URL || "http://localhost:8060"
  const apiKey = process.env.INTARIS_API_KEY || ""
  const agentId = process.env.INTARIS_AGENT_ID || "opencode"
  const userId = process.env.INTARIS_USER_ID || ""
  const failOpen =
    (process.env.INTARIS_FAIL_OPEN || "false").toLowerCase() === "true"
  const intentionOverride = process.env.INTARIS_INTENTION || ""
  const workingDirectory = worktree || directory || ""
  const rawInterval = parseInt(
    process.env.INTARIS_CHECKPOINT_INTERVAL || "25",
    10,
  )
  const checkpointInterval = isNaN(rawInterval) ? 25 : rawInterval

  // -- State ----------------------------------------------------------------
  // Track Intaris session per OpenCode session.
  // Bounded to prevent unbounded growth in long-running instances.
  const MAX_SESSIONS = 100
  const MAX_RECENT_TOOLS = 10
  const sessions = new Map<string, SessionState>()

  // -- API Client -----------------------------------------------------------

  interface ApiResult {
    data: any | null
    error: string | null
    status: number | null
  }

  async function callApi(
    method: string,
    path: string,
    payload: object,
    timeoutMs: number = 5000,
  ): Promise<ApiResult> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Agent-Id": agentId,
    }
    if (apiKey) {
      headers["Authorization"] = `Bearer ${apiKey}`
    }
    if (userId) {
      headers["X-User-Id"] = userId
    }

    try {
      const resp = await fetch(`${baseUrl}${path}`, {
        method,
        headers,
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      })
      if (resp.ok) return { data: await resp.json(), error: null, status: resp.status }

      // Non-OK response — extract server error detail
      const body = await resp.text().catch(() => "")
      let detail = `HTTP ${resp.status}`
      try {
        const parsed = JSON.parse(body)
        detail = parsed.detail || parsed.error || detail
      } catch {
        if (body) detail = body.slice(0, 200)
      }

      await client.app
        .log({
          body: {
            service: "intaris",
            level: "warn",
            message: `API ${method} ${path} returned ${resp.status}: ${detail}`,
            extra: { status: resp.status, body: body.slice(0, 200) },
          },
        })
        .catch(() => {})
      return { data: null, error: detail, status: resp.status }
    } catch (err) {
      await client.app
        .log({
          body: {
            service: "intaris",
            level: "warn",
            message: `API ${method} ${path} failed: ${err}`,
          },
        })
        .catch(() => {})
      return { data: null, error: String(err), status: null }
    }
  }

  // -- Helpers --------------------------------------------------------------

  function getOrCreateState(sessionId: string): SessionState {
    let state = sessions.get(sessionId)
    if (!state) {
      state = {
        intarisSessionId: null,
        sessionCreated: false,
        callCount: 0,
        approvedCount: 0,
        deniedCount: 0,
        escalatedCount: 0,
        recentTools: [],
        parentSessionId: null,
        lastError: null,
        agentName: null,
        sessionTitle: null,
        intentionUpdated: false,
      }
      sessions.set(sessionId, state)
      // Evict oldest entries if over limit
      if (sessions.size > MAX_SESSIONS) {
        const excess = sessions.size - MAX_SESSIONS
        let count = 0
        for (const key of sessions.keys()) {
          if (count >= excess) break
          sessions.delete(key)
          count++
        }
      }
    }
    return state
  }

  /**
   * Build the session intention string.
   * Uses Session.title if available, falls back to working directory.
   */
  function buildIntention(state: SessionState): string {
    if (intentionOverride) return intentionOverride

    // For child sessions, include agent type and parent reference
    if (state.parentSessionId) {
      const agent = state.agentName || "sub-agent"
      const title = state.sessionTitle
      if (title) {
        return `OpenCode ${agent}: ${title}`
      }
      return `OpenCode ${agent} session in ${workingDirectory || "unknown"}`
    }

    // For main sessions, use title if available
    if (state.sessionTitle) {
      return state.sessionTitle
    }

    if (workingDirectory) {
      return `OpenCode coding session in ${workingDirectory}`
    }
    return "OpenCode coding session"
  }

  /**
   * Build session details including agent type.
   */
  function buildDetails(state: SessionState): Record<string, any> {
    const details: Record<string, any> = {
      source: "opencode",
      working_directory: workingDirectory,
    }
    if (state.agentName) {
      details.agent_type = state.agentName
    }
    return details
  }

  /**
   * Update the session intention on the server after gathering context.
   * Called after the first few tool calls to refine the generic intention.
   */
  function updateIntention(
    intarisSessionId: string,
    state: SessionState,
  ): void {
    if (state.intentionUpdated) return
    if (intentionOverride) return
    // Only update after enough calls to have context
    if (state.callCount < 3) return

    state.intentionUpdated = true

    const intention = buildIntention(state)
    const details = buildDetails(state)

    callApi(
      "PATCH",
      `/api/v1/session/${intarisSessionId}`,
      { intention, details },
      2000,
    ).catch(() => {})
  }

  /**
   * Build a checkpoint content string with enriched session statistics.
   */
  function buildCheckpointContent(state: SessionState): string {
    const interval = Math.floor(state.callCount / checkpointInterval)
    const tools = state.recentTools.join(", ") || "none"
    return (
      `Checkpoint #${interval}: ${state.callCount} calls ` +
      `(${state.approvedCount} approved, ${state.deniedCount} denied, ` +
      `${state.escalatedCount} escalated). Recent tools: ${tools}`
    )
  }

  /**
   * Build an agent summary string with session statistics.
   */
  function buildAgentSummary(state: SessionState): string {
    const agent = state.agentName ? ` (${state.agentName})` : ""
    return (
      `OpenCode session${agent} completed. ${state.callCount} tool calls ` +
      `(${state.approvedCount} approved, ${state.deniedCount} denied, ` +
      `${state.escalatedCount} escalated). ` +
      `Working directory: ${workingDirectory || "unknown"}`
    )
  }

  /**
   * Ensure an Intaris session exists for the given OpenCode session.
   * Creates one via POST /api/v1/intention if needed.
   * Returns the Intaris session_id, or null on failure.
   */
  async function ensureSession(
    sessionId: string,
    state: SessionState,
  ): Promise<string | null> {
    if (state.intarisSessionId) return state.intarisSessionId

    // Generate a deterministic Intaris session ID from the OpenCode session
    const intarisSessionId = `oc-${sessionId}`

    const intentionBody: Record<string, any> = {
      session_id: intarisSessionId,
      intention: buildIntention(state),
      details: buildDetails(state),
    }

    // Include parent_session_id for child sessions (session continuation chains)
    if (state.parentSessionId) {
      intentionBody.parent_session_id = state.parentSessionId
    }

    const { data, error, status } = await callApi(
      "POST",
      "/api/v1/intention",
      intentionBody,
      2000, // 2s timeout for session creation (leaves headroom for evaluate)
    )

    if (data) {
      state.intarisSessionId = intarisSessionId
      state.sessionCreated = true
      await client.app
        .log({
          body: {
            service: "intaris",
            level: "info",
            message: `Session created: ${intarisSessionId}`,
          },
        })
        .catch(() => {})
    } else if (status !== null && status >= 400 && status < 500) {
      // Client error (auth, validation) — propagate detail, don't retry
      state.lastError = error || `HTTP ${status}`
      return null
    } else {
      // Session may already exist (409 conflict) or server error — try using it anyway
      state.intarisSessionId = intarisSessionId
    }

    return state.intarisSessionId
  }

  /**
   * Send a periodic checkpoint to Intaris (fire-and-forget).
   */
  function sendCheckpoint(
    intarisSessionId: string,
    state: SessionState,
  ): void {
    if (checkpointInterval <= 0) return
    if (state.callCount % checkpointInterval !== 0) return

    callApi(
      "POST",
      "/api/v1/checkpoint",
      {
        session_id: intarisSessionId,
        content: buildCheckpointContent(state),
      },
      2000,
    ).catch(() => {})
  }

  /**
   * Signal session completion to Intaris (fire-and-forget).
   * Sends status update and agent summary in parallel.
   */
  function signalCompletion(
    intarisSessionId: string,
    state: SessionState,
  ): void {
    // Fire both calls in parallel — neither blocks the other
    Promise.all([
      callApi(
        "PATCH",
        `/api/v1/session/${intarisSessionId}/status`,
        { status: "completed" },
        2000,
      ),
      callApi(
        "POST",
        `/api/v1/session/${intarisSessionId}/agent-summary`,
        { summary: buildAgentSummary(state) },
        2000,
      ),
    ]).catch(() => {})
  }

  /**
   * Find and complete all child sessions of a given parent.
   */
  function completeChildSessions(parentIntarisId: string): void {
    for (const [childId, childState] of sessions) {
      if (childState.parentSessionId === parentIntarisId && childState.intarisSessionId) {
        signalCompletion(childState.intarisSessionId, childState)
        sessions.delete(childId)
      }
    }
  }

  // -- Initialization -------------------------------------------------------

  if (!apiKey) {
    await client.app
      .log({
        body: {
          service: "intaris",
          level: "warn",
          message:
            "INTARIS_API_KEY not set — plugin will fail to authenticate",
        },
      })
      .catch(() => {})
  }

  if (failOpen) {
    await client.app
      .log({
        body: {
          service: "intaris",
          level: "warn",
          message:
            "INTARIS_FAIL_OPEN=true — tool calls will proceed unchecked if Intaris is unreachable",
        },
      })
      .catch(() => {})
  }

  await client.app
    .log({
      body: {
        service: "intaris",
        level: "info",
        message: "Plugin initialized",
        extra: { baseUrl, agentId, failOpen, checkpointInterval },
      },
    })
    .catch(() => {})

  // -- Hooks ----------------------------------------------------------------

  return {
    // -- Agent Name Capture -------------------------------------------------
    "chat.message": async (
      input: {
        sessionID: string
        agent?: string
        model?: { providerID: string; modelID: string }
        messageID?: string
      },
      _output: any,
    ) => {
      if (!input.sessionID || !input.agent) return
      const state = sessions.get(input.sessionID)
      if (state && !state.agentName) {
        state.agentName = input.agent
        // If session already created, update intention with agent info
        if (state.intarisSessionId && !state.intentionUpdated) {
          state.intentionUpdated = true
          const intention = buildIntention(state)
          const details = buildDetails(state)
          callApi(
            "PATCH",
            `/api/v1/session/${state.intarisSessionId}`,
            { intention, details },
            2000,
          ).catch(() => {})
        }
      }
    },

    // -- Session Lifecycle --------------------------------------------------
    event: async ({ event }: { event: { type: string; properties: any } }) => {
      if (event.type === "session.created") {
        const sessionId: string = event.properties?.info?.id
        if (!sessionId) return

        const state = getOrCreateState(sessionId)

        // Capture session title for smarter intention
        const title: string | undefined = event.properties?.info?.title
        if (title) {
          state.sessionTitle = title
        }

        // Track parent session for child sessions (subagent tasks).
        // Child sessions are created with parent_session_id for session
        // chain analysis, rather than being skipped.
        const parentID: string | undefined = event.properties?.info?.parentID
        if (parentID) {
          state.parentSessionId = `oc-${parentID}`
        }

        // Pre-create the Intaris session (best-effort, non-blocking)
        ensureSession(sessionId, state).catch(() => {})
      }

      // Signal session completion when explicitly deleted.
      if (event.type === "session.deleted") {
        const sessionId: string = event.properties?.info?.id
        if (!sessionId) return

        const state = sessions.get(sessionId)
        if (!state?.intarisSessionId) return

        // Complete this session
        signalCompletion(state.intarisSessionId, state)
        // Also complete any child sessions that are still active
        completeChildSessions(state.intarisSessionId)
        sessions.delete(sessionId)
      }

      // Handle session.idle — complete child sessions that go idle.
      // OpenCode fires this when a session becomes idle (no more activity).
      // For child sessions (sub-agents), idle means the task is done.
      if (event.type === "session.idle") {
        const sessionId: string = event.properties?.sessionID
        if (!sessionId) return

        const state = sessions.get(sessionId)
        if (!state?.intarisSessionId) return

        // Only auto-complete child sessions on idle, not parent sessions
        if (state.parentSessionId) {
          signalCompletion(state.intarisSessionId, state)
          sessions.delete(sessionId)
        }
      }
    },

    // -- Tool Interception --------------------------------------------------
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      _output: { args: any },
    ) => {
      const { tool, sessionID } = input
      if (!sessionID) return

      const state = getOrCreateState(sessionID)

      // Ensure session exists (lazy creation for resumed sessions)
      const intarisSessionId = await ensureSession(sessionID, state)
      if (!intarisSessionId) {
        if (failOpen) return
        const detail = state.lastError || "unknown error"
        throw new Error(
          `[intaris] Cannot create session: ${detail}`,
        )
      }

      // Evaluate the tool call
      const { data: result, error: evalError, status: evalStatus } = await callApi(
        "POST",
        "/api/v1/evaluate",
        {
          session_id: intarisSessionId,
          tool,
          args: _output.args || {},
        },
        5000, // 5s timeout for evaluation
      )

      if (!result) {
        // Distinguish config errors (4xx) from transient failures (5xx/network)
        if (evalStatus !== null && evalStatus >= 400 && evalStatus < 500) {
          throw new Error(
            `[intaris] Evaluation rejected for ${tool}: ${evalError}`,
          )
        }
        // Intaris unreachable or server error
        if (failOpen) {
          await client.app
            .log({
              body: {
                service: "intaris",
                level: "warn",
                message: `Evaluate failed for ${tool} — allowing (fail-open)`,
              },
            })
            .catch(() => {})
          return
        }
        throw new Error(
          `[intaris] Evaluation failed for ${tool}: ${evalError || "server unreachable"} (INTARIS_FAIL_OPEN=false)`,
        )
      }

      // Track decision statistics
      state.callCount++
      if (result.decision === "approve") state.approvedCount++
      else if (result.decision === "deny") state.deniedCount++
      else if (result.decision === "escalate") state.escalatedCount++

      // Track recent tool names (bounded to last MAX_RECENT_TOOLS)
      state.recentTools = [...state.recentTools, tool].slice(-MAX_RECENT_TOOLS)

      await client.app
        .log({
          body: {
            service: "intaris",
            level: "info",
            message: `${tool}: ${result.decision} (${result.path}, ${result.latency_ms}ms)`,
            extra: {
              call_id: result.call_id,
              risk: result.risk,
            },
          },
        })
        .catch(() => {})

      // Send periodic checkpoint (fire-and-forget, non-blocking)
      sendCheckpoint(intarisSessionId, state)

      // Try to update intention after gathering enough context
      updateIntention(intarisSessionId, state)

      if (result.decision === "deny") {
        const reason = result.reasoning || "Tool call denied by safety evaluation"
        throw new Error(
          `[intaris] DENIED: ${reason}`,
        )
      }

      if (result.decision === "escalate") {
        const reason =
          result.reasoning ||
          "Tool call requires human approval"
        throw new Error(
          `[intaris] ESCALATED (${result.call_id}): ${reason}\n` +
            `Approve or deny this call in the Intaris UI, then retry.`,
        )
      }

      // decision === "approve" — tool call proceeds normally
    },
  }
}
