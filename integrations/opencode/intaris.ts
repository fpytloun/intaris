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
 * 4. session.updated: Updates Intaris intention when session title changes
 * 5. session.deleted / session.idle: Signals session completion to Intaris
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
 *   INTARIS_ALLOW_PATHS          - Comma-separated parent directories to allow reads from (e.g., ~/src)
 *   INTARIS_CHECKPOINT_INTERVAL  - Evaluate calls between checkpoints (default: 25, 0=disabled)
 *   INTARIS_ESCALATION_TIMEOUT   - Max seconds to wait for escalation approval (default: 0=no timeout)
 *   INTARIS_SESSION_RECORDING    - Enable session recording (default: false)
 *   INTARIS_RECORDING_FLUSH_SIZE - Events per recording batch (default: 50)
 *   INTARIS_RECORDING_FLUSH_MS   - Recording flush interval in ms (default: 10000)
 */

import type { Plugin } from "@opencode-ai/plugin"

interface RecordingEvent {
  type: string
  data: Record<string, any>
}

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
  intentionPending: boolean
  isIdle: boolean
  // Recording buffer
  recordingBuffer: RecordingEvent[]
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
  const rawEscalationTimeout = parseInt(
    process.env.INTARIS_ESCALATION_TIMEOUT || "0",
    10,
  )
  const escalationTimeoutMs = isNaN(rawEscalationTimeout)
    ? 0
    : Math.max(0, rawEscalationTimeout * 1000)
  const sessionRecording =
    (process.env.INTARIS_SESSION_RECORDING || "false").toLowerCase() === "true"
  const rawRecordingFlushSize = parseInt(
    process.env.INTARIS_RECORDING_FLUSH_SIZE || "50",
    10,
  )
  const recordingFlushSize = isNaN(rawRecordingFlushSize) ? 50 : rawRecordingFlushSize
  const rawRecordingFlushMs = parseInt(
    process.env.INTARIS_RECORDING_FLUSH_MS || "10000",
    10,
  )
  const recordingFlushMs = isNaN(rawRecordingFlushMs) ? 10000 : rawRecordingFlushMs
  const allowPathsRaw = process.env.INTARIS_ALLOW_PATHS || ""

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
    payload: object | null,
    timeoutMs: number = 5000,
    extraHeaders?: Record<string, string>,
  ): Promise<ApiResult> {
    const headers: Record<string, string> = {
      "X-Agent-Id": agentId,
      ...extraHeaders,
    }
    if (payload !== null) {
      headers["Content-Type"] = "application/json"
    }
    if (apiKey) {
      headers["Authorization"] = `Bearer ${apiKey}`
    }
    if (userId) {
      headers["X-User-Id"] = userId
    }

    try {
      const fetchOptions: RequestInit = {
        method,
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      }
      if (payload !== null) {
        fetchOptions.body = JSON.stringify(payload)
      }
      const resp = await fetch(`${baseUrl}${path}`, fetchOptions)
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

  /**
   * Call API with retries and exponential backoff.
   * Retries on network errors and 5xx responses. Does NOT retry 4xx.
   */
  async function callApiWithRetry(
    method: string,
    path: string,
    payload: object | null,
    timeoutMs: number = 30000,
    maxRetries: number = 3,
  ): Promise<ApiResult> {
    const backoffMs = [1000, 2000, 4000]
    let lastResult: ApiResult = { data: null, error: "no attempts", status: null }

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      lastResult = await callApi(method, path, payload, timeoutMs)

      // Success — return immediately
      if (lastResult.data !== null) return lastResult

      // 4xx client errors — do not retry (auth, validation, not found)
      if (lastResult.status !== null && lastResult.status >= 400 && lastResult.status < 500) {
        return lastResult
      }

      // 5xx or network error — retry with backoff (unless last attempt)
      if (attempt < maxRetries) {
        const delay = backoffMs[Math.min(attempt, backoffMs.length - 1)]
        await client.app
          .log({
            body: {
              service: "intaris",
              level: "warn",
              message: `API ${method} ${path} failed (attempt ${attempt + 1}/${maxRetries + 1}), retrying in ${delay}ms`,
            },
          })
          .catch(() => {})
        await new Promise((resolve) => setTimeout(resolve, delay))
      }
    }

    return lastResult
  }

  // -- Toast Notifications --------------------------------------------------

  /**
   * Show a visible toast notification in the OpenCode TUI.
   * Fire-and-forget — never blocks or throws.
   */
  function showToast(
    message: string,
    variant: "info" | "success" | "warning" | "error",
    duration?: number,
  ): void {
    client.tui
      .showToast({
        body: {
          title: "Intaris",
          message,
          variant,
          duration: duration ?? (variant === "error" ? 8000 : 5000),
        },
      })
      .catch(() => {})
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
        intentionPending: false,
        isIdle: false,
        recordingBuffer: [],
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
   * Build session policy from INTARIS_ALLOW_PATHS.
   * Expands ~ to home directory and converts each path to a glob pattern.
   * E.g., "~/src" → "/Users/name/src/*"
   *
   * Always includes ~/.local/share/opencode/* (tool output storage)
   * so reads of truncated output files are never blocked.
   */
  function buildPolicy(): Record<string, any> | null {
    const home = process.env.HOME || process.env.USERPROFILE || ""
    // Always allow reads from OpenCode's tool output directory
    const builtinPaths = home
      ? [`${home}/.local/share/opencode/*`]
      : []
    const userPatterns = allowPathsRaw
      ? allowPathsRaw
          .split(",")
          .map((p) => p.trim())
          .filter(Boolean)
          .map((p) => {
            // Expand ~ to home directory
            if (p.startsWith("~/") || p === "~") {
              p = home + p.slice(1)
            }
            // Ensure trailing /* for glob matching
            if (!p.endsWith("*")) {
              p = p.endsWith("/") ? p + "*" : p + "/*"
            }
            return p
          })
      : []
    const patterns = [...builtinPaths, ...userPatterns]
    if (patterns.length === 0) return null
    return { allow_paths: patterns }
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
      `/api/v1/session/${encodeURIComponent(intarisSessionId)}`,
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

  // -- Recording Helpers ---------------------------------------------------

  /**
   * Queue a recording event for a session. Events are buffered and
   * flushed in batches to reduce API calls. No-op if recording is disabled.
   */
  function recordEvent(
    sessionId: string,
    event: RecordingEvent,
  ): void {
    if (!sessionRecording) return
    const state = sessions.get(sessionId)
    if (!state?.intarisSessionId) return

    state.recordingBuffer.push(event)

    // Auto-flush when buffer reaches threshold
    if (state.recordingBuffer.length >= recordingFlushSize) {
      flushRecordingBuffer(sessionId)
    }
  }

  /**
   * Flush the recording buffer for a session (fire-and-forget).
   * Sends buffered events to POST /session/{id}/events.
   */
  function flushRecordingBuffer(sessionId: string): void {
    const state = sessions.get(sessionId)
    if (!state?.intarisSessionId) return
    if (state.recordingBuffer.length === 0) return

    // Drain the buffer
    const events = state.recordingBuffer.splice(0)

    callApi(
      "POST",
      `/api/v1/session/${encodeURIComponent(state.intarisSessionId)}/events`,
      events,
      5000,
      { "X-Intaris-Source": "opencode" },
    ).then(({ error, status }) => {
      if (error) {
        client.app
          .log({
            body: {
              service: "intaris",
              level: "warn",
              message: `Recording flush failed for ${state!.intarisSessionId}: ${error} (HTTP ${status})`,
              extra: { eventCount: events.length },
            },
          })
          .catch(() => {})
      }
    }).catch(() => {})
  }

  // Periodic recording flush timer (flushes all sessions)
  let recordingFlushTimer: ReturnType<typeof setInterval> | null = null
  if (sessionRecording) {
    recordingFlushTimer = setInterval(() => {
      for (const [sessionId] of sessions) {
        flushRecordingBuffer(sessionId)
      }
    }, recordingFlushMs)
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

    // Include allow_paths policy from INTARIS_ALLOW_PATHS
    const policy = buildPolicy()
    if (policy) {
      intentionBody.policy = policy
    }

    // Include parent_session_id for child sessions (session continuation chains)
    if (state.parentSessionId) {
      intentionBody.parent_session_id = state.parentSessionId
    }

    const { data, error, status } = await callApiWithRetry(
      "POST",
      "/api/v1/intention",
      intentionBody,
      5000, // 5s timeout for session creation
      2,    // 2 retries
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
    } else if (status === 409) {
      // Session already exists (resumed OpenCode session) — reuse it
      state.intarisSessionId = intarisSessionId
      await client.app
        .log({
          body: {
            service: "intaris",
            level: "info",
            message: `Session already exists, reusing: ${intarisSessionId}`,
          },
        })
        .catch(() => {})

      // Re-activate the session (may have been swept to idle/completed)
      // and update intention in case the title changed since creation
      callApi(
        "PATCH",
        `/api/v1/session/${encodeURIComponent(intarisSessionId)}/status`,
        { status: "active" },
        2000,
      ).catch(() => {})
      callApi(
        "PATCH",
        `/api/v1/session/${encodeURIComponent(intarisSessionId)}`,
        { intention: buildIntention(state), details: buildDetails(state) },
        2000,
      ).catch(() => {})
    } else if (status !== null && status >= 400 && status < 500) {
      // Client error (auth, validation) — propagate detail, don't retry
      state.lastError = error || `HTTP ${status}`
      return null
    } else {
      // Server error or network issue — try using it anyway
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
   * Flushes recording buffer, then sends status update and agent summary.
   */
  function signalCompletion(
    intarisSessionId: string,
    state: SessionState,
    sessionId?: string,
  ): void {
    // Flush recording buffer before completion
    if (sessionId) {
      flushRecordingBuffer(sessionId)
    }

    // Fire both calls in parallel — neither blocks the other
    Promise.all([
      callApi(
        "PATCH",
        `/api/v1/session/${encodeURIComponent(intarisSessionId)}/status`,
        { status: "completed" },
        2000,
      ),
      callApi(
        "POST",
        `/api/v1/session/${encodeURIComponent(intarisSessionId)}/agent-summary`,
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
        signalCompletion(childState.intarisSessionId, childState, childId)
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
        extra: { baseUrl, agentId, failOpen, checkpointInterval, sessionRecording },
      },
    })
    .catch(() => {})

  // -- Hooks ----------------------------------------------------------------

  return {
    // -- Agent Name Capture + User Message Forwarding -----------------------
    "chat.message": async (
      input: {
        sessionID: string
        agent?: string
        model?: { providerID: string; modelID: string }
        messageID?: string
      },
      output: { message?: any; parts?: any[] },
    ) => {
      if (!input.sessionID) return
      // Do NOT use getOrCreateState() here — creating state before
      // session.created fires causes a race condition where the session
      // is created on the server without parent_session_id (the parent
      // link is only set in session.created). If no state exists yet,
      // skip this event; the session will be created properly later.
      const state = sessions.get(input.sessionID)
      if (!state) return

      // Capture agent name on first message
      if (input.agent && !state.agentName) {
        state.agentName = input.agent
        // If session already created, update intention with agent info
        if (state.intarisSessionId && !state.intentionUpdated) {
          state.intentionUpdated = true
          const intention = buildIntention(state)
          const details = buildDetails(state)
          callApi(
            "PATCH",
            `/api/v1/session/${encodeURIComponent(state.intarisSessionId)}`,
            { intention, details },
            2000,
          ).catch(() => {})
        }
      }

      // Resume session from idle when user provides new input.
      // This transitions the Intaris session back to active before any
      // tool calls execute, making the UI reflect that work is starting.
      if (state.isIdle && state.intarisSessionId) {
        state.isIdle = false
        callApi(
          "PATCH",
          `/api/v1/session/${encodeURIComponent(state.intarisSessionId)}/status`,
          { status: "active" },
          2000,
        ).catch(() => {})
      }

      // Extract user message text from parts and send as reasoning record.
      // This gives Intaris visibility into what the user is asking the agent
      // to do, enabling better intention tracking and safety evaluation.
      if (state.intarisSessionId && output?.parts && Array.isArray(output.parts)) {
        const userText = output.parts
          .filter((p: any) => p.type === "text" && !p.synthetic)
          .map((p: any) => p.text)
          .join("\n")
          .trim()

        if (userText) {
          callApi(
            "POST",
            "/api/v1/reasoning",
            {
              session_id: state.intarisSessionId,
              content: `User message: ${userText}`,
            },
            2000,
          ).catch(() => {})

          // Signal that an intention update is in flight. The next
          // tool.execute.before will include intention_pending=true in
          // the evaluate request so the server waits for the /reasoning
          // call to arrive and the intention to be updated before
          // evaluating. Cleared after the first evaluate call returns.
          state.intentionPending = true

          // Record user message for session recording
          recordEvent(input.sessionID, {
            type: "message",
            data: {
              role: "user",
              text: userText,
              agent: input.agent,
              model: input.model,
              messageID: input.messageID,
              sessionID: input.sessionID,
            },
          })
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

      // Update Intaris intention when session title changes.
      // OpenCode starts with a generic title and updates it after the
      // first user message (e.g., "New session" → "Fix login bug").
      if (event.type === "session.updated") {
        const sessionId: string = event.properties?.info?.id
        if (!sessionId) return

        const state = sessions.get(sessionId)
        if (!state?.intarisSessionId) return

        const title: string | undefined = event.properties?.info?.title
        if (title && title !== state.sessionTitle) {
          state.sessionTitle = title
          callApi(
            "PATCH",
            `/api/v1/session/${encodeURIComponent(state.intarisSessionId)}`,
            { intention: buildIntention(state), details: buildDetails(state) },
            2000,
          ).catch(() => {})
        }
      }

      // Signal session completion when explicitly deleted.
      if (event.type === "session.deleted") {
        const sessionId: string = event.properties?.info?.id
        if (!sessionId) return

        const state = sessions.get(sessionId)
        if (!state?.intarisSessionId) return

        // Complete this session
        signalCompletion(state.intarisSessionId, state, sessionId)
        // Also complete any child sessions that are still active
        completeChildSessions(state.intarisSessionId)
        sessions.delete(sessionId)
      }

      // Handle session.idle — transition parent sessions to idle,
      // complete child sessions. OpenCode fires this when a session
      // becomes idle (waiting for user input, no more activity).
      if (event.type === "session.idle") {
        const sessionId: string = event.properties?.sessionID
        if (!sessionId) return

        const state = sessions.get(sessionId)
        if (!state?.intarisSessionId) return

        if (state.parentSessionId) {
          // Child sessions (sub-agents): idle means the task is done
          signalCompletion(state.intarisSessionId, state, sessionId)
          sessions.delete(sessionId)
        } else {
          // Parent sessions: transition to idle so the UI shows the
          // session is waiting for user input, not actively working
          if (!state.isIdle) {
            state.isIdle = true
            callApi(
              "PATCH",
              `/api/v1/session/${encodeURIComponent(state.intarisSessionId)}/status`,
              { status: "idle" },
              2000,
            ).catch(() => {})
          }
        }
      }

      // -- Session Recording: capture part updates --------------------------
      // message.part.updated fires for every part creation/update including
      // streaming deltas. Captures assistant text, step-start/finish, tool
      // invocations, and all other part types for full session fidelity.
      if (event.type === "message.part.updated") {
        const part = event.properties?.part
        if (!part) return

        const sessionId: string = part.sessionID
        if (!sessionId) return

        recordEvent(sessionId, {
          type: "part",
          data: {
            sessionID: sessionId,
            messageID: part.messageID,
            part,
            delta: event.properties?.delta,
          },
        })
      }

      // message.updated captures full message metadata (role, model, tokens)
      if (event.type === "message.updated") {
        const info = event.properties?.info
        if (!info) return

        const sessionId: string = info.sessionID
        if (!sessionId) return

        recordEvent(sessionId, {
          type: "message",
          data: {
            sessionID: sessionId,
            messageID: info.id,
            role: info.role,
            model: "modelID" in info ? info.modelID : undefined,
            metadata: info,
          },
        })
      }
    },

    // -- Tool Interception --------------------------------------------------
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      _output: { args: any },
    ) => {
      const { tool, sessionID } = input
      if (!sessionID) return

      // Skip evaluation for invalid/unavailable tool calls.
      // OpenCode fires tool.execute.before even when the model tries to
      // call a tool that doesn't exist — the args contain an error message.
      // No point evaluating these; they'll fail on their own.
      const args = _output.args || {}
      if (typeof args.error === "string" && args.error.includes("unavailable tool")) {
        return
      }

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

      // Record tool call event (before evaluation, fire-and-forget)
      recordEvent(sessionID, {
        type: "tool_call",
        data: {
          tool,
          args: _output.args || {},
          callID: input.callID,
          sessionID,
        },
      })

      // Evaluate the tool call (30s timeout, retries with backoff).
      // When intentionPending is true, the server waits for the
      // /reasoning call to arrive before evaluating (race condition fix).
      const intentionPending = state.intentionPending
      const { data: result, error: evalError, status: evalStatus } = await callApiWithRetry(
        "POST",
        "/api/v1/evaluate",
        {
          session_id: intarisSessionId,
          tool,
          args: _output.args || {},
          ...(intentionPending && { intention_pending: true }),
        },
        30000, // 30s timeout for evaluation (large tool calls can be slow)
        2,     // 2 retries (3 total attempts, worst case ~90s)
      )

      // Clear the flag after the first evaluate call — subsequent tool
      // calls in this turn benefit from the already-updated intention.
      if (intentionPending) {
        state.intentionPending = false
      }

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
        // Handle session-level suspension: wait for user action
        // (reactivation or termination) rather than dying immediately.
        if (result.session_status === "suspended") {
          const statusReason = result.status_reason || "Session suspended"
          await client.app
            .log({
              body: {
                service: "intaris",
                level: "warn",
                message: `Session suspended: ${statusReason}. Waiting for approval in Intaris UI...`,
              },
            })
            .catch(() => {})
          showToast(
            `Session suspended: ${statusReason}. Reactivate in Intaris UI.`,
            "warning",
            10000,
          )

          // Poll GET /session/{id} with exponential backoff
          const suspendBackoffMs = [2000, 4000, 8000, 16000, 30000]
          const suspendStart = Date.now()
          let suspendAttempt = 0
          let suspendLastReminder = suspendStart

          while (true) {
            // Check timeout (reuse escalation timeout)
            if (escalationTimeoutMs > 0 && Date.now() - suspendStart > escalationTimeoutMs) {
              throw new Error(
                `[intaris] SESSION SUSPENSION TIMEOUT: ${statusReason}\n` +
                  `No response within ${rawEscalationTimeout}s. Reactivate or terminate in the Intaris UI.`,
              )
            }

            // Periodic reminder every 60s
            const suspendNow = Date.now()
            if (suspendNow - suspendLastReminder >= 60000) {
              const waitSec = Math.round((suspendNow - suspendStart) / 1000)
              await client.app
                .log({
                  body: {
                    service: "intaris",
                    level: "warn",
                    message: `Still waiting for session approval... ${waitSec}s elapsed. Reason: ${statusReason}`,
                  },
                })
                .catch(() => {})
              showToast(
                `Session still suspended... (${waitSec}s)`,
                "info",
              )
              suspendLastReminder = suspendNow
            }

            // Wait with exponential backoff
            const suspendDelay = suspendBackoffMs[Math.min(suspendAttempt, suspendBackoffMs.length - 1)]
            await new Promise((resolve) => setTimeout(resolve, suspendDelay))
            suspendAttempt++

            // Poll session status
            const { data: sessionData } = await callApi(
              "GET",
              `/api/v1/session/${encodeURIComponent(intarisSessionId)}`,
              null,
              5000,
            )

            if (!sessionData) continue // Server unreachable — keep polling

            if (sessionData.status === "active") {
              // Session reactivated — re-evaluate this tool call.
              // The user explicitly approved the session, but we still
              // need to evaluate this specific tool call for safety.
              await client.app
                .log({
                  body: {
                    service: "intaris",
                    level: "info",
                    message: `Session reactivated — re-evaluating ${tool}`,
                  },
                })
                .catch(() => {})
              showToast("Session reactivated — re-evaluating...", "success")

              const { data: reResult } = await callApiWithRetry(
                "POST",
                "/api/v1/evaluate",
                {
                  session_id: intarisSessionId,
                  tool,
                  args: _output.args || {},
                },
                30000,
                2,
              )

              if (!reResult) {
                if (failOpen) return
                throw new Error(`[intaris] Re-evaluation failed for ${tool} after session reactivation`)
              }

              if (reResult.decision === "deny") {
                throw new Error(`[intaris] DENIED: ${reResult.reasoning || "Tool call denied after session reactivation"}`)
              }
              if (reResult.decision === "escalate") {
                // Fall through to the escalation handling below would be
                // complex; for simplicity, treat post-reactivation escalation
                // as a deny. The user just reactivated — they can retry.
                throw new Error(`[intaris] ESCALATED after reactivation: ${reResult.reasoning || "Requires human approval"}`)
              }
              // Approved — let tool proceed
              return
            }

            if (sessionData.status === "terminated") {
              showToast("Session terminated", "error")
              throw new Error(
                `[intaris] Session terminated: ${sessionData.status_reason || "terminated by user"}`,
              )
            }
            // Still suspended — continue polling
          }
        }

        // Handle session termination: hard kill
        if (result.session_status === "terminated") {
          showToast("Session terminated", "error")
          throw new Error(
            `[intaris] Session terminated: ${result.status_reason || "terminated by user"}`,
          )
        }

        const reason = result.reasoning || "Tool call denied by safety evaluation"
        showToast(`Tool "${tool}" denied: ${reason}`, "error")
        throw new Error(
          `[intaris] DENIED: ${reason}`,
        )
      }

      if (result.decision === "escalate") {
        const reason =
          result.reasoning ||
          "Tool call requires human approval"

        // Log escalation and wait for user approval via polling
        await client.app
          .log({
            body: {
              service: "intaris",
              level: "warn",
              message: `ESCALATED ${tool} (${result.call_id}): ${reason}. Waiting for approval in Intaris UI...`,
            },
          })
          .catch(() => {})
        showToast(
          `Tool "${tool}" escalated — approve or deny in Intaris UI.\n${reason}`,
          "warning",
          10000,
        )

        // Poll for user decision with exponential backoff
        const pollBackoffMs = [2000, 4000, 8000, 16000, 30000]
        const startTime = Date.now()
        let pollAttempt = 0
        let lastReminderAt = startTime

        while (true) {
          // Check timeout (0 = no timeout)
          if (escalationTimeoutMs > 0 && Date.now() - startTime > escalationTimeoutMs) {
            throw new Error(
              `[intaris] ESCALATION TIMEOUT (${result.call_id}): ${reason}\n` +
                `No response within ${rawEscalationTimeout}s. Approve or deny in the Intaris UI.`,
            )
          }

          // Periodic reminder every 60s so operators know the agent is waiting
          const now = Date.now()
          if (now - lastReminderAt >= 60000) {
            const waitSec = Math.round((now - startTime) / 1000)
            await client.app
              .log({
                body: {
                  service: "intaris",
                  level: "warn",
                  message: `Still waiting for escalation approval for ${tool} (${result.call_id})... ${waitSec}s elapsed`,
                },
              })
              .catch(() => {})
            showToast(
              `Still waiting for approval of "${tool}"... (${waitSec}s)`,
              "info",
            )
            lastReminderAt = now
          }

          // Wait with exponential backoff (capped at 30s)
          const delay = pollBackoffMs[Math.min(pollAttempt, pollBackoffMs.length - 1)]
          await new Promise((resolve) => setTimeout(resolve, delay))
          pollAttempt++

          // Check if the escalation has been resolved
          const { data: auditRecord } = await callApi(
            "GET",
            `/api/v1/audit/${encodeURIComponent(result.call_id)}`,
            null,
            5000,
          )

          if (!auditRecord) continue // Server unreachable — keep polling

          if (auditRecord.user_decision === "approve") {
            await client.app
              .log({
                body: {
                  service: "intaris",
                  level: "info",
                  message: `Escalation approved: ${tool} (${result.call_id})`,
                },
              })
              .catch(() => {})
            showToast(`Tool "${tool}" approved — proceeding`, "success")
            break // Approved — let the tool call proceed
          }

          if (auditRecord.user_decision === "deny") {
            const denyNote = auditRecord.user_note
              ? ` — ${auditRecord.user_note}`
              : ""
            showToast(`Tool "${tool}" denied by user`, "error")
            throw new Error(
              `[intaris] DENIED by user (${result.call_id}): ${reason}${denyNote}`,
            )
          }

          // No decision yet — continue polling
        }
      }

      // decision === "approve" (or escalation approved) — tool call proceeds normally
    },

    // -- Tool Result Recording ------------------------------------------------
    "tool.execute.after": async (
      input: {
        tool: string
        sessionID: string
        callID: string
      },
      output: {
        output?: any
        isError?: boolean
        title?: string
        metadata?: Record<string, any>
      },
    ) => {
      if (!input.sessionID) return

      recordEvent(input.sessionID, {
        type: "tool_result",
        data: {
          tool: input.tool,
          callID: input.callID,
          sessionID: input.sessionID,
          output: output.output,
          isError: output.isError || false,
          title: output.title,
          metadata: output.metadata,
        },
      })
    },
  }
}
