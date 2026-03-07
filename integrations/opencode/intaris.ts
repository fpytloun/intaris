/**
 * intaris — Guardrails plugin for OpenCode.
 *
 * This plugin intercepts every tool call and evaluates it through
 * Intaris's safety pipeline before allowing execution. Tool calls
 * that are denied or escalated are blocked with an error message.
 *
 * Flow:
 * 1. session.created: Creates an Intaris session via POST /api/v1/intention
 * 2. tool.execute.before: Evaluates every tool call via POST /api/v1/evaluate
 *    - approve: tool executes normally
 *    - deny: throws error with reasoning (blocks execution)
 *    - escalate: throws error directing user to Intaris UI for approval
 *
 * Configuration via environment variables:
 *   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
 *   INTARIS_API_KEY    - API key for authentication (required)
 *   INTARIS_AGENT_ID   - Agent ID (default: opencode)
 *   INTARIS_USER_ID    - User ID (optional if API key maps to user)
 *   INTARIS_FAIL_OPEN  - Allow tool calls if Intaris is unreachable (default: false)
 *   INTARIS_INTENTION  - Session intention override (default: auto-generated)
 */

import type { Plugin } from "@opencode-ai/plugin"

interface SessionState {
  intarisSessionId: string | null
  sessionCreated: boolean
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

  // -- State ----------------------------------------------------------------
  // Track Intaris session per OpenCode session.
  // Bounded to prevent unbounded growth in long-running instances.
  const MAX_SESSIONS = 100
  const sessions = new Map<string, SessionState>()

  // -- API Client -----------------------------------------------------------

  async function callApi(
    method: string,
    path: string,
    payload: object,
    timeoutMs: number = 5000,
  ): Promise<any | null> {
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
      if (resp.ok) return await resp.json()

      // Log non-OK responses
      const body = await resp.text().catch(() => "")
      await client.app
        .log({
          body: {
            service: "intaris",
            level: "warn",
            message: `API ${method} ${path} returned ${resp.status}`,
            extra: { status: resp.status, body: body.slice(0, 200) },
          },
        })
        .catch(() => {})
      return null
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
      return null
    }
  }

  // -- Helpers --------------------------------------------------------------

  function getOrCreateState(sessionId: string): SessionState {
    let state = sessions.get(sessionId)
    if (!state) {
      state = {
        intarisSessionId: null,
        sessionCreated: false,
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
   */
  function buildIntention(): string {
    if (intentionOverride) return intentionOverride
    if (workingDirectory) {
      return `OpenCode coding session in ${workingDirectory}`
    }
    return "OpenCode coding session"
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

    const result = await callApi(
      "POST",
      "/api/v1/intention",
      {
        session_id: intarisSessionId,
        intention: buildIntention(),
        details: {
          source: "opencode",
          working_directory: workingDirectory,
        },
      },
      2000, // 2s timeout for session creation (leaves headroom for evaluate)
    )

    if (result) {
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
    } else {
      // Session may already exist (409 conflict) — try using it anyway
      state.intarisSessionId = intarisSessionId
    }

    return state.intarisSessionId
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
        extra: { baseUrl, agentId, failOpen },
      },
    })
    .catch(() => {})

  // -- Hooks ----------------------------------------------------------------

  return {
    // -- Session Lifecycle --------------------------------------------------
    event: async ({ event }: { event: { type: string; properties: any } }) => {
      if (event.type === "session.created") {
        const sessionId: string = event.properties?.info?.id
        if (!sessionId) return

        // Skip child sessions (subagent tasks)
        if (event.properties?.info?.parentID) return

        const state = getOrCreateState(sessionId)
        // Pre-create the Intaris session (best-effort, non-blocking)
        ensureSession(sessionId, state).catch(() => {})
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
        throw new Error(
          "[intaris] Cannot create session — tool call blocked (INTARIS_FAIL_OPEN=false)",
        )
      }

      // Evaluate the tool call
      const result: EvaluateResponse | null = await callApi(
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
        // Intaris unreachable
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
          `[intaris] Evaluation failed for ${tool} — tool call blocked (INTARIS_FAIL_OPEN=false)`,
        )
      }

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
