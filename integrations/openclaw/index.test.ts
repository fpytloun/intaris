import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import intarisPlugin from "./index.js";

// Helper to build a mock fetch response
function mockResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("intaris plugin", () => {
  // Capture registered hooks
  const hooks: Record<string, Function> = {};
  const api = {
    pluginConfig: {
      url: "http://intaris.test:8060",
      apiKey: "test-key",
    },
    logger: {
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
      debug: vi.fn(),
    },
    on: vi.fn((hookName: string, handler: Function) => {
      hooks[hookName] = handler;
    }),
  };

  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    for (const key of Object.keys(hooks)) delete hooks[key];
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn() as unknown as typeof globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("registers expected hooks", () => {
    intarisPlugin.register(api as any);

    expect(api.on).toHaveBeenCalledWith("session_start", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("before_reset", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("before_agent_start", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("before_tool_call", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("after_tool_call", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("llm_output", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("agent_end", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("session_end", expect.any(Function));
    expect(api.on).toHaveBeenCalledWith("gateway_stop", expect.any(Function));
  });

  it("has correct plugin metadata", () => {
    expect(intarisPlugin.id).toBe("intaris");
    expect(intarisPlugin.name).toBe("Intaris Guardrails");
  });

  describe("config resolution", () => {
    it("uses env vars as fallback", () => {
      process.env.INTARIS_URL = "http://env-intaris:9090";
      process.env.INTARIS_API_KEY = "env-key";

      const envApi = {
        ...api,
        pluginConfig: {},
      };
      intarisPlugin.register(envApi as any);

      // Verify the URL was picked up by checking the fetch call target
      // when session_start fires
      expect(api.logger.info).toHaveBeenCalled();

      delete process.env.INTARIS_URL;
      delete process.env.INTARIS_API_KEY;
    });

    it("logs warning when API key is missing", () => {
      const noKeyApi = {
        ...api,
        pluginConfig: { url: "http://intaris.test:8060" },
      };
      intarisPlugin.register(noKeyApi as any);

      expect(noKeyApi.logger.warn).toHaveBeenCalledWith(
        expect.stringContaining("API key not configured"),
      );
    });

    it("logs warning when failOpen is enabled", () => {
      const failOpenApi = {
        ...api,
        pluginConfig: { url: "http://intaris.test:8060", apiKey: "k", failOpen: true },
      };
      intarisPlugin.register(failOpenApi as any);

      expect(failOpenApi.logger.warn).toHaveBeenCalledWith(
        expect.stringContaining("Fail-open mode enabled"),
      );
    });
  });

  describe("session_start", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("creates an Intaris session via POST /api/v1/intention", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-main", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-1", sessionKey: "main" },
        { agentId: "default", sessionKey: "main", sessionId: "sess-1" },
      );

      // Wait for the async ensureSession to complete
      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      const [url, opts] = vi.mocked(globalThis.fetch).mock.calls[0];
      expect(url).toBe("http://intaris.test:8060/api/v1/intention");
      expect(opts?.method).toBe("POST");

      const body = JSON.parse(opts?.body as string);
      expect(body.session_id).toMatch(
        /^oc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
      );
      expect(body.intention).toContain("OpenClaw");

      // Verify X-Agent-Id header comes from ctx.agentId
      const headers = opts?.headers as Record<string, string>;
      expect(headers["X-Agent-Id"]).toBe("default");
    });

    it("reuses existing session on 409 conflict", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ detail: "already exists" }, 409),
      );

      await hooks.session_start(
        { sessionId: "sess-2", sessionKey: "main" },
        { agentId: "default", sessionKey: "main", sessionId: "sess-2" },
      );

      // session_start fires ensureSession as fire-and-forget, so wait for
      // the log call that happens after the fetch response is processed.
      await vi.waitFor(() => {
        expect(api.logger.info).toHaveBeenCalledWith(
          expect.stringContaining("already exists, reusing"),
        );
      });
    });
  });

  describe("before_reset hook", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("signals completion and removes session state on reset", async () => {
      // Setup: create a session via before_tool_call (which awaits ensureSession,
      // unlike session_start which fires it as fire-and-forget).
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ status: "active" })) // createIntention
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c-r1",
            decision: "approve",
            reasoning: "ok",
            risk: "low",
            path: "fast",
            latency_ms: 1,
          }),
        ); // evaluate

      await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc-r1" },
        { agentId: "default", sessionKey: "sk-r1", toolName: "bash" },
      );

      // Clear call history so we only see completion calls
      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}, 200));

      // Fire before_reset
      await hooks.before_reset({}, { agentId: "default", sessionKey: "sk-r1" });

      // signalCompletion fires PATCH /status and POST /agent-summary as
      // fire-and-forget promises, so wait for them to land.
      await vi.waitFor(() => {
        expect(vi.mocked(globalThis.fetch).mock.calls.length).toBeGreaterThanOrEqual(2);
      });

      const urls = vi.mocked(globalThis.fetch).mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.includes("/status"))).toBe(true);
      expect(urls.some((u) => u.includes("/agent-summary"))).toBe(true);
    });

    it("does nothing when sessionKey is absent", async () => {
      const fetchBefore = vi.mocked(globalThis.fetch).mock.calls.length;
      await hooks.before_reset({}, {});
      expect(vi.mocked(globalThis.fetch).mock.calls.length).toBe(fetchBefore);
    });

    it("does nothing when no state exists for the session key", async () => {
      const fetchBefore = vi.mocked(globalThis.fetch).mock.calls.length;
      await hooks.before_reset({}, { sessionKey: "nonexistent" });
      expect(vi.mocked(globalThis.fetch).mock.calls.length).toBe(fetchBefore);
    });

    it("after reset, session_start creates a new distinct Intaris session ID", async () => {
      // First session — use before_tool_call to ensure ensureSession is awaited
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ status: "active" })) // createIntention
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c-r2",
            decision: "approve",
            reasoning: "ok",
            risk: "low",
            path: "fast",
            latency_ms: 1,
          }),
        ); // evaluate

      await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc-r2" },
        { agentId: "default", sessionKey: "sk-r2", toolName: "bash" },
      );

      // Capture the first session ID from the createIntention call
      const intentionCalls1 = vi
        .mocked(globalThis.fetch)
        .mock.calls.filter(([url]) => String(url).includes("/intention"));
      expect(intentionCalls1.length).toBe(1);
      const firstId = JSON.parse(intentionCalls1[0][1]?.body as string).session_id;

      // Reset
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}, 200));
      await hooks.before_reset({}, { agentId: "default", sessionKey: "sk-r2" });

      // New session after reset — again use before_tool_call
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ status: "active" })) // createIntention
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c-r3",
            decision: "approve",
            reasoning: "ok",
            risk: "low",
            path: "fast",
            latency_ms: 1,
          }),
        ); // evaluate

      await hooks.before_tool_call(
        { toolName: "bash", params: { command: "pwd" }, toolCallId: "tc-r3" },
        { agentId: "default", sessionKey: "sk-r2", toolName: "bash" },
      );

      const intentionCalls2 = vi
        .mocked(globalThis.fetch)
        .mock.calls.filter(([url]) => String(url).includes("/intention"));
      // Should have 2 intention calls total (first session + after reset)
      expect(intentionCalls2.length).toBe(2);
      const secondId = JSON.parse(intentionCalls2[1][1]?.body as string).session_id;

      expect(secondId).not.toBe(firstId);
      expect(secondId).toMatch(/^oc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
    });
  });

  describe("before_tool_call", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("approves tool call when Intaris returns approve", async () => {
      // First call: createIntention (session creation)
      // Second call: evaluate
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ session_id: "oc-sk1", status: "active" }))
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c1",
            decision: "approve",
            reasoning: "safe",
            risk: "low",
            path: "fast",
            latency_ms: 12,
          }),
        );

      const result = await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc1" },
        { agentId: "default", sessionKey: "sk1", toolName: "bash" },
      );

      expect(result).toEqual({});
    });

    it("blocks tool call when Intaris returns deny", async () => {
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ session_id: "oc-sk2", status: "active" }))
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c2",
            decision: "deny",
            reasoning: "dangerous command",
            risk: "critical",
            path: "fast",
            latency_ms: 5,
          }),
        );

      const result = await hooks.before_tool_call(
        { toolName: "bash", params: { command: "rm -rf /" }, toolCallId: "tc2" },
        { agentId: "default", sessionKey: "sk2", toolName: "bash" },
      );

      expect(result).toEqual({
        block: true,
        blockReason: expect.stringContaining("DENIED: dangerous command"),
      });
    });

    it("blocks when Intaris is unreachable and failOpen is false", async () => {
      vi.mocked(globalThis.fetch).mockRejectedValue(new Error("ECONNREFUSED"));

      const result = await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc3" },
        { agentId: "default", sessionKey: "sk3", toolName: "bash" },
      );

      // When Intaris is unreachable, ensureSession falls through to the
      // server-error branch and sets the session ID anyway. The subsequent
      // evaluate call also fails, producing "Evaluation failed".
      expect(result).toEqual({
        block: true,
        blockReason: expect.stringContaining("INTARIS_FAIL_OPEN=false"),
      });
    });

    it("allows when Intaris is unreachable and failOpen is true", async () => {
      const failOpenApi = {
        ...api,
        pluginConfig: {
          url: "http://intaris.test:8060",
          apiKey: "test-key",
          failOpen: true,
        },
      };
      // Re-register with failOpen
      for (const key of Object.keys(hooks)) delete hooks[key];
      intarisPlugin.register(failOpenApi as any);

      vi.mocked(globalThis.fetch).mockRejectedValue(new Error("ECONNREFUSED"));

      const result = await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc4" },
        { agentId: "default", sessionKey: "sk4", toolName: "bash" },
      );

      expect(result).toEqual({});
    });

    it("passes ctx.agentId as X-Agent-Id header to evaluate", async () => {
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ session_id: "oc-sk5", status: "active" }))
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c5",
            decision: "approve",
            reasoning: "ok",
            risk: "low",
            path: "fast",
            latency_ms: 3,
          }),
        );

      await hooks.before_tool_call(
        { toolName: "read", params: { path: "/tmp/x" }, toolCallId: "tc5" },
        { agentId: "my-agent-42", sessionKey: "sk5", toolName: "read" },
      );

      // The evaluate call is the second fetch call
      const [, evalOpts] = vi.mocked(globalThis.fetch).mock.calls[1];
      const headers = evalOpts?.headers as Record<string, string>;
      expect(headers["X-Agent-Id"]).toBe("my-agent-42");
    });

    it("blocks on 4xx evaluation error even with failOpen", async () => {
      const failOpenApi = {
        ...api,
        pluginConfig: {
          url: "http://intaris.test:8060",
          apiKey: "test-key",
          failOpen: true,
        },
      };
      for (const key of Object.keys(hooks)) delete hooks[key];
      intarisPlugin.register(failOpenApi as any);

      // Session creation succeeds, but evaluate returns 403
      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ session_id: "oc-sk6", status: "active" }))
        .mockResolvedValueOnce(mockResponse({ detail: "forbidden" }, 403));

      const result = await hooks.before_tool_call(
        { toolName: "bash", params: { command: "ls" }, toolCallId: "tc6" },
        { agentId: "default", sessionKey: "sk6", toolName: "bash" },
      );

      expect(result).toEqual({
        block: true,
        blockReason: expect.stringContaining("Evaluation rejected"),
      });
    });
  });

  describe("before_agent_start (reasoning context)", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("forwards user prompt as reasoning context", async () => {
      // First: session creation via session_start
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-sk7", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-7", sessionKey: "sk7" },
        { agentId: "default", sessionKey: "sk7", sessionId: "sess-7" },
      );

      // Wait for session creation
      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.before_agent_start(
        { prompt: "List all files in /tmp" },
        { agentId: "default", sessionKey: "sk7" },
      );

      // Should have called /api/v1/reasoning
      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      expect(reasoningCall).toBeDefined();

      const body = JSON.parse(reasoningCall![1]?.body as string);
      expect(body.content).toContain("List all files in /tmp");
    });

    it("skips empty prompts", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-sk8", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-8", sessionKey: "sk8" },
        { agentId: "default", sessionKey: "sk8", sessionId: "sess-8" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      vi.mocked(globalThis.fetch).mockClear();

      await hooks.before_agent_start({ prompt: "   " }, { agentId: "default", sessionKey: "sk8" });

      // No reasoning call should have been made
      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      expect(reasoningCall).toBeUndefined();
    });
  });

  describe("llm_output (assistant context tracking)", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("stores last assistant text from llm_output", async () => {
      // Create session first
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-ctx1", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-ctx1", sessionKey: "ctx1" },
        { agentId: "default", sessionKey: "ctx1", sessionId: "sess-ctx1" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      // Fire llm_output with assistant texts
      await hooks.llm_output(
        { assistantTexts: ["I can refactor the database module for you."] },
        { agentId: "default", sessionKey: "ctx1" },
      );

      // Now fire before_agent_start — the context should be included
      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.before_agent_start(
        { prompt: "ok, do it" },
        { agentId: "default", sessionKey: "ctx1" },
      );

      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      expect(reasoningCall).toBeDefined();

      const body = JSON.parse(reasoningCall![1]?.body as string);
      expect(body.context).toBe("I can refactor the database module for you.");
      expect(body.content).toContain("ok, do it");
    });

    it("uses last text when multiple assistantTexts are present", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-ctx2", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-ctx2", sessionKey: "ctx2" },
        { agentId: "default", sessionKey: "ctx2", sessionId: "sess-ctx2" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      await hooks.llm_output(
        { assistantTexts: ["First response", "Second response", "Final response"] },
        { agentId: "default", sessionKey: "ctx2" },
      );

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.before_agent_start({ prompt: "yes" }, { agentId: "default", sessionKey: "ctx2" });

      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      const body = JSON.parse(reasoningCall![1]?.body as string);
      expect(body.context).toBe("Final response");
    });

    it("consumes context after sending (cleared for next turn)", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-ctx3", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-ctx3", sessionKey: "ctx3" },
        { agentId: "default", sessionKey: "ctx3", sessionId: "sess-ctx3" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      // Set assistant context
      await hooks.llm_output(
        { assistantTexts: ["Some context"] },
        { agentId: "default", sessionKey: "ctx3" },
      );

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      // First user message — should include context
      await hooks.before_agent_start(
        { prompt: "do it" },
        { agentId: "default", sessionKey: "ctx3" },
      );

      const firstCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      const firstBody = JSON.parse(firstCall![1]?.body as string);
      expect(firstBody.context).toBe("Some context");

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      // Second user message — context should be cleared
      await hooks.before_agent_start(
        { prompt: "another message" },
        { agentId: "default", sessionKey: "ctx3" },
      );

      const secondCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      const secondBody = JSON.parse(secondCall![1]?.body as string);
      expect(secondBody.context).toBeUndefined();
    });

    it("does not send context when no assistant text was captured", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-ctx4", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-ctx4", sessionKey: "ctx4" },
        { agentId: "default", sessionKey: "ctx4", sessionId: "sess-ctx4" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      // No llm_output fired — context should not be in the request
      await hooks.before_agent_start(
        { prompt: "hello" },
        { agentId: "default", sessionKey: "ctx4" },
      );

      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      const body = JSON.parse(reasoningCall![1]?.body as string);
      expect(body.context).toBeUndefined();
    });

    it("ignores llm_output with empty assistantTexts", async () => {
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-ctx5", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-ctx5", sessionKey: "ctx5" },
        { agentId: "default", sessionKey: "ctx5", sessionId: "sess-ctx5" },
      );

      await vi.waitFor(() => {
        expect(globalThis.fetch).toHaveBeenCalled();
      });

      // Fire llm_output with empty array
      await hooks.llm_output({ assistantTexts: [] }, { agentId: "default", sessionKey: "ctx5" });

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.before_agent_start(
        { prompt: "hello" },
        { agentId: "default", sessionKey: "ctx5" },
      );

      const reasoningCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/api/v1/reasoning"));
      const body = JSON.parse(reasoningCall![1]?.body as string);
      expect(body.context).toBeUndefined();
    });
  });

  describe("after_tool_call", () => {
    it("does not throw on missing session", async () => {
      intarisPlugin.register(api as any);

      // Should not throw even without a session
      await hooks.after_tool_call(
        { toolName: "bash", toolCallId: "tc-x", error: undefined, durationMs: 100 },
        { agentId: "default", sessionKey: "no-session", toolName: "bash" },
      );
    });
  });

  describe("agent_end", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("transitions session to idle", async () => {
      // Create session first
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-sk9", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-9", sessionKey: "sk9" },
        { agentId: "default", sessionKey: "sk9", sessionId: "sess-9" },
      );

      // session_start fires ensureSession as fire-and-forget; wait for the
      // "Session created" log which confirms state.intarisSessionId is set.
      await vi.waitFor(() => {
        expect(api.logger.info).toHaveBeenCalledWith(expect.stringContaining("Session created"));
      });

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.agent_end({}, { agentId: "default", sessionKey: "sk9" });

      // updateStatus is fire-and-forget (.catch(() => {})), wait for it
      await vi.waitFor(() => {
        const statusCall = vi
          .mocked(globalThis.fetch)
          .mock.calls.find(([url]) => String(url).includes("/status"));
        expect(statusCall).toBeDefined();
      });

      const statusCall = vi
        .mocked(globalThis.fetch)
        .mock.calls.find(([url]) => String(url).includes("/status"));
      const body = JSON.parse(statusCall![1]?.body as string);
      expect(body.status).toBe("idle");
    });
  });

  describe("session_end", () => {
    beforeEach(() => {
      intarisPlugin.register(api as any);
    });

    it("signals completion to Intaris", async () => {
      // Create session
      vi.mocked(globalThis.fetch).mockResolvedValue(
        mockResponse({ session_id: "oc-sk10", status: "active" }),
      );

      await hooks.session_start(
        { sessionId: "sess-10", sessionKey: "sk10" },
        { agentId: "default", sessionKey: "sk10", sessionId: "sess-10" },
      );

      // session_start fires ensureSession as fire-and-forget; wait for the
      // "Session created" log which confirms state.intarisSessionId is set.
      await vi.waitFor(() => {
        expect(api.logger.info).toHaveBeenCalledWith(expect.stringContaining("Session created"));
      });

      vi.mocked(globalThis.fetch).mockClear();
      vi.mocked(globalThis.fetch).mockResolvedValue(mockResponse({}));

      await hooks.session_end(
        { sessionId: "sess-10" },
        { agentId: "default", sessionKey: "sk10", sessionId: "sess-10" },
      );

      // signalCompletion fires status + summary as fire-and-forget, wait for both
      await vi.waitFor(() => {
        const calls = vi.mocked(globalThis.fetch).mock.calls;
        const statusCall = calls.find(([url]) => String(url).includes("/status"));
        const summaryCall = calls.find(([url]) => String(url).includes("/agent-summary"));
        expect(statusCall).toBeDefined();
        expect(summaryCall).toBeDefined();
      });

      const calls = vi.mocked(globalThis.fetch).mock.calls;
      const statusCall = calls.find(([url]) => String(url).includes("/status"));
      const statusBody = JSON.parse(statusCall![1]?.body as string);
      expect(statusBody.status).toBe("completed");
    });
  });

  describe("no agentId in config", () => {
    it("does not send X-Agent-Id header when ctx.agentId is undefined", async () => {
      intarisPlugin.register(api as any);

      vi.mocked(globalThis.fetch)
        .mockResolvedValueOnce(mockResponse({ session_id: "oc-sk-no-agent", status: "active" }))
        .mockResolvedValueOnce(
          mockResponse({
            call_id: "c-no-agent",
            decision: "approve",
            reasoning: "ok",
            risk: "low",
            path: "fast",
            latency_ms: 1,
          }),
        );

      await hooks.before_tool_call(
        { toolName: "read", params: { path: "/tmp" }, toolCallId: "tc-na" },
        { sessionKey: "sk-no-agent", toolName: "read" }, // no agentId
      );

      // Check that X-Agent-Id is NOT in headers
      for (const [, opts] of vi.mocked(globalThis.fetch).mock.calls) {
        const headers = opts?.headers as Record<string, string>;
        expect(headers["X-Agent-Id"]).toBeUndefined();
      }
    });
  });
});
