/**
 * TypeScript types for the Intaris REST API.
 */

// -- Configuration ----------------------------------------------------------

export interface IntarisConfig {
  url: string;
  apiKey: string;
  userId: string;
  failOpen: boolean;
  allowPaths: string;
  escalationTimeoutMs: number;
  checkpointInterval: number;
  recording: boolean;
  recordingFlushSize: number;
  recordingFlushMs: number;
  /** Record full tool output in session recording events (default: follows `recording`). */
  recordToolOutput: boolean;
  /** Enable MCP tool proxy — fetches tools from Intaris MCP servers and registers them as agent tools. */
  mcpTools: boolean;
  /** Cache TTL for the MCP tool list in milliseconds (default: 900000 = 15 min). */
  mcpToolsCacheTtlMs: number;
}

// -- API Types --------------------------------------------------------------

export interface ApiResult {
  data: Record<string, unknown> | null;
  error: string | null;
  status: number | null;
}

export interface EvaluateRequest {
  session_id: string;
  tool: string;
  args: Record<string, unknown>;
  intention_pending?: boolean;
}

export interface EvaluateResponse {
  call_id: string;
  decision: "approve" | "deny" | "escalate";
  reasoning?: string;
  risk?: string;
  path: string;
  latency_ms: number;
  session_status?: string;
  status_reason?: string;
}

export interface IntentionRequest {
  session_id: string;
  intention: string;
  details?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  parent_session_id?: string;
}

export interface SessionResponse {
  session_id: string;
  status: string;
  status_reason?: string;
  intention?: string;
}

export interface AuditRecord {
  call_id: string;
  user_decision?: "approve" | "deny" | null;
  user_note?: string | null;
}

// -- MCP Types --------------------------------------------------------------

/** MCP tool definition as returned by the Intaris backend. */
export interface McpToolDef {
  /** MCP server name (e.g., "github") */
  server: string;
  /** MCP tool name (e.g., "create_issue") */
  name: string;
  /** Human-readable display name */
  title?: string;
  /** Tool description for the LLM */
  description?: string;
  /** JSON Schema for tool input parameters */
  inputSchema: Record<string, unknown>;
}

/** Result of an MCP tool call proxied through Intaris. */
export interface McpCallResult {
  content: Array<{ type: string; text?: string; [key: string]: unknown }>;
  isError?: boolean;
  /** Safety evaluation decision. */
  decision?: "approve" | "deny" | "escalate";
  /** Audit call ID (present for deny/escalate decisions). */
  call_id?: string;
  /** Evaluation reasoning (present for deny/escalate decisions). */
  reasoning?: string;
  latency_ms?: number;
}

/** Cached MCP tool list with fetch timestamp. */
export interface McpToolCache {
  tools: McpToolDef[];
  fetchedAt: number;
}

// -- Plugin State -----------------------------------------------------------

export interface RecordingEvent {
  type: string;
  data: Record<string, unknown>;
}

export interface SessionState {
  intarisSessionId: string | null;
  sessionCreated: boolean;
  callCount: number;
  approvedCount: number;
  deniedCount: number;
  escalatedCount: number;
  recentTools: string[];
  lastError: string | null;
  intentionPending: boolean;
  intentionUpdated: boolean;
  isIdle: boolean;
  recordingBuffer: RecordingEvent[];
  /** Last assistant response text, consumed as context for the next reasoning call */
  lastAssistantText: string;
  /** In-flight session creation promise (prevents duplicate creation). */
  creating?: Promise<string | null>;
  /** Dedup: last reasoning prompt submitted (prevents double submission from dual plugin instances). */
  lastReasoningPrompt?: string;
  /** Dedup: tool call IDs already evaluated (prevents double evaluation from dual plugin instances). */
  evaluatedToolCalls?: Set<string>;
  /** Dedup: tool call IDs already recorded (prevents double recording from dual plugin instances). */
  recordedToolResults?: Set<string>;
  /** Dedup: last assistant text recorded (prevents double recording from dual plugin instances). */
  lastRecordedAssistantText?: string;
  /** Parent Intaris session ID for sub-agent sessions. */
  parentIntarisSessionId?: string;
  /** Sub-agent label (e.g. "research", "code-review"). */
  subagentLabel?: string;
  /** Sub-agent spawn mode ("run" = one-shot, "session" = persistent). */
  subagentMode?: string;
}
