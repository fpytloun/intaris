/**
 * HTTP client for the Intaris REST API.
 *
 * Provides typed wrappers around fetch() with retry logic, exponential
 * backoff, and proper error handling. All methods are fire-and-forget
 * safe (never throw on network errors when used with .catch(() => {})).
 */

import type {
  ApiResult,
  IntarisConfig,
  McpCallResult,
  McpToolDef,
  RecordingEvent,
} from "./types.js";

type Logger = (
  level: "info" | "warn" | "error",
  message: string,
  extra?: Record<string, unknown>,
) => void;

export class IntarisClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly userId: string;
  private readonly log: Logger;

  constructor(config: IntarisConfig, log: Logger) {
    this.baseUrl = config.url.replace(/\/+$/, "");
    this.apiKey = config.apiKey;
    this.userId = config.userId;
    this.log = log;
  }

  // -- Low-level API --------------------------------------------------------

  async callApi(
    method: string,
    path: string,
    payload: object | null,
    timeoutMs: number = 5000,
    extraHeaders?: Record<string, string>,
    agentId?: string,
  ): Promise<ApiResult> {
    const headers: Record<string, string> = {
      ...extraHeaders,
    };
    if (agentId) {
      headers["X-Agent-Id"] = agentId;
    }
    if (payload !== null) {
      headers["Content-Type"] = "application/json";
    }
    if (this.apiKey) {
      headers["Authorization"] = `Bearer ${this.apiKey}`;
    }
    if (this.userId) {
      headers["X-User-Id"] = this.userId;
    }

    try {
      const fetchOptions: RequestInit = {
        method,
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      };
      if (payload !== null) {
        fetchOptions.body = JSON.stringify(payload);
      }
      const resp = await fetch(`${this.baseUrl}${path}`, fetchOptions);
      if (resp.ok)
        return {
          data: (await resp.json()) as Record<string, unknown>,
          error: null,
          status: resp.status,
        };

      // Non-OK response -- extract server error detail
      const body = await resp.text().catch(() => "");
      let detail = `HTTP ${resp.status}`;
      try {
        const parsed = JSON.parse(body) as Record<string, unknown>;
        detail = String(parsed.detail || parsed.error || detail);
      } catch {
        if (body) detail = body.slice(0, 200);
      }

      this.log("warn", `API ${method} ${path} returned ${resp.status}: ${detail}`, {
        status: resp.status,
        body: body.slice(0, 200),
      });
      return { data: null, error: detail, status: resp.status };
    } catch (err) {
      this.log("warn", `API ${method} ${path} failed: ${err}`);
      return { data: null, error: String(err), status: null };
    }
  }

  /**
   * Call API with retries and exponential backoff.
   * Retries on network errors and 5xx responses. Does NOT retry 4xx.
   */
  async callApiWithRetry(
    method: string,
    path: string,
    payload: object | null,
    timeoutMs: number = 30000,
    maxRetries: number = 3,
    agentId?: string,
  ): Promise<ApiResult> {
    const backoffMs = [1000, 2000, 4000];
    let lastResult: ApiResult = { data: null, error: "no attempts", status: null };

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      lastResult = await this.callApi(method, path, payload, timeoutMs, undefined, agentId);

      // Success -- return immediately
      if (lastResult.data !== null) return lastResult;

      // 4xx client errors -- do not retry (auth, validation, not found)
      if (lastResult.status !== null && lastResult.status >= 400 && lastResult.status < 500) {
        return lastResult;
      }

      // 5xx or network error -- retry with backoff (unless last attempt)
      if (attempt < maxRetries) {
        const delay = backoffMs[Math.min(attempt, backoffMs.length - 1)];
        this.log(
          "warn",
          `API ${method} ${path} failed (attempt ${attempt + 1}/${maxRetries + 1}), retrying in ${delay}ms`,
        );
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }

    return lastResult;
  }

  // -- High-level API -------------------------------------------------------

  async createIntention(
    sessionId: string,
    intention: string,
    details: Record<string, unknown>,
    policy: Record<string, unknown> | null,
    parentSessionId: string | null,
    agentId?: string,
  ): Promise<ApiResult> {
    const body: Record<string, unknown> = {
      session_id: sessionId,
      intention,
      details,
    };
    if (policy) body.policy = policy;
    if (parentSessionId) body.parent_session_id = parentSessionId;

    return this.callApiWithRetry("POST", "/api/v1/intention", body, 5000, 2, agentId);
  }

  async evaluate(
    sessionId: string,
    tool: string,
    args: Record<string, unknown>,
    intentionPending: boolean,
    agentId?: string,
  ): Promise<ApiResult> {
    const body: Record<string, unknown> = {
      session_id: sessionId,
      tool,
      args,
    };
    if (intentionPending) body.intention_pending = true;

    return this.callApiWithRetry("POST", "/api/v1/evaluate", body, 30000, 2, agentId);
  }

  async updateStatus(sessionId: string, status: string, agentId?: string): Promise<ApiResult> {
    return this.callApi(
      "PATCH",
      `/api/v1/session/${encodeURIComponent(sessionId)}/status`,
      { status },
      2000,
      undefined,
      agentId,
    );
  }

  async updateSession(
    sessionId: string,
    intention: string,
    details: Record<string, unknown>,
    agentId?: string,
  ): Promise<ApiResult> {
    return this.callApi(
      "PATCH",
      `/api/v1/session/${encodeURIComponent(sessionId)}`,
      { intention, details },
      2000,
      undefined,
      agentId,
    );
  }

  async getSession(sessionId: string, agentId?: string): Promise<ApiResult> {
    return this.callApi(
      "GET",
      `/api/v1/session/${encodeURIComponent(sessionId)}`,
      null,
      5000,
      undefined,
      agentId,
    );
  }

  async getAudit(callId: string, agentId?: string): Promise<ApiResult> {
    return this.callApi(
      "GET",
      `/api/v1/audit/${encodeURIComponent(callId)}`,
      null,
      5000,
      undefined,
      agentId,
    );
  }

  async submitAgentSummary(
    sessionId: string,
    summary: string,
    agentId?: string,
  ): Promise<ApiResult> {
    return this.callApi(
      "POST",
      `/api/v1/session/${encodeURIComponent(sessionId)}/agent-summary`,
      { summary },
      2000,
      undefined,
      agentId,
    );
  }

  async submitCheckpoint(sessionId: string, content: string, agentId?: string): Promise<ApiResult> {
    return this.callApi(
      "POST",
      "/api/v1/checkpoint",
      { session_id: sessionId, content },
      2000,
      undefined,
      agentId,
    );
  }

  async submitReasoning(
    sessionId: string,
    content: string,
    agentId?: string,
    context?: string,
    fromEvents?: boolean,
  ): Promise<ApiResult> {
    const body: Record<string, unknown> = { session_id: sessionId, content };
    if (context) body.context = context;
    if (fromEvents) body.from_events = true;
    return this.callApi("POST", "/api/v1/reasoning", body, 2000, undefined, agentId);
  }

  async appendEvents(
    sessionId: string,
    events: RecordingEvent[],
    agentId?: string,
  ): Promise<ApiResult> {
    return this.callApi(
      "POST",
      `/api/v1/session/${encodeURIComponent(sessionId)}/events`,
      events as unknown as object,
      5000,
      { "X-Intaris-Source": "openclaw" },
      agentId,
    );
  }

  // -- MCP API --------------------------------------------------------------

  /**
   * Fetch the list of available MCP tools from all configured MCP servers.
   * Returns an empty array on failure (never throws).
   */
  async listMcpTools(agentId?: string): Promise<McpToolDef[]> {
    const result = await this.callApi("GET", "/api/v1/mcp/tools", null, 10000, undefined, agentId);
    if (!result.data) {
      return [];
    }
    const tools = (result.data as { tools?: unknown }).tools;
    if (!Array.isArray(tools)) {
      this.log("warn", "MCP tools response missing 'tools' array");
      return [];
    }
    return tools as McpToolDef[];
  }

  /**
   * Proxy a tool call to an MCP server via Intaris.
   * Uses retry logic since MCP tool execution can be slow.
   */
  async callMcpTool(
    sessionId: string,
    server: string,
    tool: string,
    args: Record<string, unknown>,
    agentId?: string,
  ): Promise<{ data: McpCallResult | null; error: string | null }> {
    const result = await this.callApiWithRetry(
      "POST",
      "/api/v1/mcp/call",
      { session_id: sessionId, server, tool, arguments: args },
      60000,
      2,
      agentId,
    );
    if (!result.data) {
      return { data: null, error: result.error };
    }
    return { data: result.data as unknown as McpCallResult, error: null };
  }
}
