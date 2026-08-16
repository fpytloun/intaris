# Claude Code Integration -- Guardrails

A hooks-based integration for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that evaluates every tool call through Intaris's safety pipeline before allowing execution.

## Integration Approaches

Intaris offers two integration approaches for Claude Code. Choose one -- do **not** use both simultaneously, or tool calls will be evaluated twice.

### Approach A: Hooks (Recommended)

Shell script hooks configured in Claude Code's `settings.json` that intercept tool calls and lifecycle events. This gives you:

- Fine-grained control over error messages
- Configurable fail-open/fail-closed behavior
- Session lifecycle management (including sub-agent sessions)
- User message forwarding for intention tracking
- Escalation polling (waits for judge/human approval)
- Session suspension and termination handling
- Debug logging via stderr
- Behavioral analysis: periodic checkpoints, session completion signals, agent summaries

### Approach B: MCP Proxy

Configure Intaris as a remote MCP server in `claude_code_config.json`. Claude Code connects to Intaris's `/mcp` endpoint, which transparently proxies all tool calls through the safety pipeline. This gives you:

- Zero hook code -- just configuration
- Full MCP proxy features (tool preferences, escalation retry, tool namespacing)
- Works with any MCP-compatible client

The trade-off is less control over the UX (no custom error messages, no fail-open option).

## Setup -- Hooks (Approach A)

### 1. Environment Variables

The hooks make direct HTTP calls to the Intaris REST API. Set these in your shell profile (`.bashrc`, `.zshrc`, etc.):

```bash
export INTARIS_URL=http://localhost:8060
export INTARIS_API_KEY=your-api-key
export INTARIS_AGENT_ID=claude-code        # optional, defaults to "claude-code"
export INTARIS_USER_ID=your-username       # optional if API key maps to user
export INTARIS_FAIL_OPEN=false             # optional, defaults to false
export INTARIS_INTENTION=""                # optional, auto-generated from cwd
export INTARIS_ALLOW_PATHS=~/src           # optional, appended after built-in safe paths
export INTARIS_CHECKPOINT_INTERVAL=25      # optional, defaults to 25 (0=disabled)
export INTARIS_HOOK_TIMEOUT=60             # optional, must match the PreToolUse timeout in hooks.json (seconds)
export INTARIS_ESCALATION_TIMEOUT=55       # optional, max seconds to wait for approval
export INTARIS_SESSION_RECORDING=false     # optional, enable session recording
export INTARIS_DEBUG=false                 # optional, enable stderr logging
```

| Variable | Default | Description |
|---|---|---|
| `INTARIS_URL` | `http://localhost:8060` | Intaris server URL |
| `INTARIS_API_KEY` | (empty) | API key for authentication. **Required** if Intaris has `INTARIS_API_KEYS` set. |
| `INTARIS_AGENT_ID` | `claude-code` | Agent ID sent to Intaris |
| `INTARIS_USER_ID` | (empty) | User ID (optional if API key maps to a user) |
| `INTARIS_FAIL_OPEN` | `false` | If `true`, tool calls proceed **even when** Intaris is unreachable or returns transient `5xx` errors. Client/auth/schema errors still block. Default is `false` (fail-closed) — and a crash anywhere in the hook before a decision is printed also denies, via a fail-closed exit trap, rather than being treated as a non-blocking hook error. |
| `INTARIS_INTENTION` | (auto) | Session intention override. Default: `"Claude Code coding session in <cwd>"`, refreshed from each user prompt on `UserPromptSubmit` if unset. |
| `INTARIS_ALLOW_PATHS` | (empty) | Comma-separated parent directories to allow reads from without LLM evaluation. Supports `~` expansion. Intaris always includes `/tmp/*`, `/var/tmp/*`, `$TMPDIR/*` when `TMPDIR` is set, and `~/.claude/plans/*` when `HOME` is set — plus each of those directories' resolved physical path (e.g. `/private/tmp/*` on macOS, where `/tmp` is a symlink) if it differs from the literal form. User entries are appended after normalization. |
| `INTARIS_CHECKPOINT_INTERVAL` | `25` | Number of evaluate calls between periodic checkpoints. Set to `0` to disable checkpoints. Each checkpoint consumes one rate limit slot. |
| `INTARIS_HOOK_TIMEOUT` | `60` | The PreToolUse hook's actual configured timeout in seconds — must match the `timeout` value for `PreToolUse` in `hooks.json`. Used to derive the internal safety budget (a 5s margin below this) so the hook always exits with its own deny/allow before Claude Code kills it outright. |
| `INTARIS_ESCALATION_TIMEOUT` | `55` | Max seconds to wait for escalation or suspension approval. The hard ceiling is `INTARIS_HOOK_TIMEOUT` minus a 5s margin. Set to `0` to use that ceiling directly. |
| `INTARIS_SESSION_RECORDING` | `false` | Enable session recording. When `true`, tool calls, results, and user messages are recorded to the event store for playback and analysis. |
| `INTARIS_DEBUG` | `false` | Enable debug logging to stderr |

### 2. Install the Hooks

Copy the hooks configuration and scripts:

```bash
# Copy scripts (including shared library)
mkdir -p ~/.claude/scripts
cp integrations/claude-code/scripts/intaris-lib.sh ~/.claude/scripts/intaris-lib.sh
cp integrations/claude-code/scripts/intaris-session.sh ~/.claude/scripts/intaris-session.sh
cp integrations/claude-code/scripts/intaris-prompt.sh ~/.claude/scripts/intaris-prompt.sh
cp integrations/claude-code/scripts/intaris-evaluate.sh ~/.claude/scripts/intaris-evaluate.sh
cp integrations/claude-code/scripts/intaris-record.sh ~/.claude/scripts/intaris-record.sh
cp integrations/claude-code/scripts/intaris-subagent.sh ~/.claude/scripts/intaris-subagent.sh
cp integrations/claude-code/scripts/intaris-subagent-stop.sh ~/.claude/scripts/intaris-subagent-stop.sh
cp integrations/claude-code/scripts/intaris-stop.sh ~/.claude/scripts/intaris-stop.sh
cp integrations/claude-code/scripts/intaris-stop-failure.sh ~/.claude/scripts/intaris-stop-failure.sh
cp integrations/claude-code/scripts/intaris-session-end.sh ~/.claude/scripts/intaris-session-end.sh
chmod +x ~/.claude/scripts/intaris-*.sh

# Copy hooks config
cp integrations/claude-code/hooks.json ~/.claude/settings.json
# Or merge with existing settings.json if you have other hooks
```

If you already have a `~/.claude/settings.json`, merge the `hooks` section from `hooks.json` into it. The hooks section includes `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart`, `SubagentStop`, `Stop`, `StopFailure`, and `SessionEnd` hooks.

### 3. Verify

Enable debug logging and run Claude Code:

```bash
export INTARIS_DEBUG=true
claude
```

Look for `[intaris]` messages in stderr:

```
[intaris] Creating session: cc-<session-id>
[intaris] Session created: cc-<session-id>
[intaris] Forwarding user message to /reasoning
[intaris] Evaluating: Bash
[intaris] Bash: approve (fast, 12ms, risk=)
[intaris] Sending checkpoint #1
[intaris] Transitioning session to idle: cc-<session-id>
```

### Migration from Previous Version

If upgrading from the previous 4-script integration:

1. Copy the new `intaris-lib.sh` shared library (required by all scripts)
2. Copy the 5 new scripts (`intaris-prompt.sh`, `intaris-subagent.sh`, `intaris-subagent-stop.sh`, `intaris-stop-failure.sh`, `intaris-session-end.sh`)
3. Replace the 4 existing scripts (they have been rewritten)
4. Update `hooks.json` / `settings.json` with the new hook entries
5. Existing state files are backward-compatible -- the evaluate script handles both old and new formats

## Setup -- MCP Proxy (Approach B)

Add to `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "intaris": {
      "type": "streamable-http",
      "url": "http://localhost:8060/mcp",
      "headers": {
        "Authorization": "Bearer your-api-key",
        "X-Agent-Id": "claude-code",
        "X-Intaris-Intention": "Claude Code coding session"
      }
    }
  }
}
```

Configure upstream MCP servers in Intaris (via the UI, REST API, or `MCP_CONFIG_FILE`). Claude Code will see all upstream tools namespaced as `server_name:tool_name`.

## How It Works

### Hooks Flow

1. **`SessionStart`** (on startup/resume/clear/compact): Creates an Intaris session via `POST /api/v1/intention` with the working directory and allow-paths policy as context. On resume (409 conflict), re-activates the existing session and re-sends the policy too (so editing `INTARIS_ALLOW_PATHS` takes effect on the next `/clear` or `/compact`, not just on a brand-new session). Stores session state as JSON in a temp file.
2. **`UserPromptSubmit`** (on every user message): Resumes the session to `active`, refreshes the session intention from the prompt text, and forwards the prompt to `POST /api/v1/reasoning` with the assistant's last response as context — entirely in a backgrounded, disowned subshell so the prompt is never held up waiting on any of it. This enables Intaris's IntentionBarrier to track what the user is asking the agent to do.
3. **`PreToolUse`** (before every tool call):
   - Loads session state from the JSON temp file (or creates one lazily)
   - Calls `POST /api/v1/evaluate` with retry and exponential backoff
   - **approve**: outputs `{}` (allow)
   - **deny**: outputs deny decision via `hookSpecificOutput` (blocks execution)
   - **escalate**: polls `GET /api/v1/audit/{call_id}` for judge/human approval; a timeout denies with a `systemMessage` and a link into the Intaris UI, since the wait is otherwise silent
   - **suspended**: polls `GET /api/v1/session/{id}` for reactivation
   - **terminated**: blocks with termination reason
   - Updates session statistics (call count, decision breakdown, recent tools, `call_id_map`) by recomputing them from the state file's current contents inside the write's lock, not a pre-`/evaluate` snapshot, so concurrent tool calls can't lose each other's increments
   - Sends periodic checkpoints via `POST /api/v1/checkpoint` (every N calls)
   - A fail-closed exit trap guarantees a decision is always emitted: if the script exits unexpectedly (missing `jq`, a crash, a future bug) before printing one, the trap forces a deny — or an allow only if `INTARIS_FAIL_OPEN=true`
4. **`PostToolUse`** (after every tool call): Records a `tool_result` event, correlated to its `PreToolUse` call via `tool_use_id` (looked up in the state file's `call_id_map`) rather than by matching tool name + arguments, which mis-attributed results under Claude Code's routine parallel tool calls. Backgrounded and disowned, like all recording calls.
5. **`SubagentStart`** (when a sub-agent spawns): Creates a child Intaris session linked to the parent via `parent_session_id`. Enables hierarchical session tracking and AlignmentBarrier enforcement.
6. **`SubagentStop`** (when a sub-agent finishes): Signals child session completion and sends an agent summary with sub-agent statistics.
7. **`Stop`** (when Claude finishes responding):
    - Stores the assistant's last response for intention context
    - On genuine final stop: transitions the parent session to `idle`, completes child sessions, uploads transcript
    - On intermediate stop (Claude continuing): stores assistant text only
8. **`StopFailure`** (on API errors): Saves the assistant's last response to the state file so intention context is preserved even after errors.
9. **`SessionEnd`** (when the session terminates): Cleans up the temp state file.

### MCP Proxy Flow

1. Claude Code connects to Intaris at `/mcp` as a Streamable HTTP MCP server.
2. `tools/list` returns aggregated tools from all upstream servers.
3. `tools/call` evaluates each call through the safety pipeline before forwarding.

## Escalation Handling

When a tool call is escalated, the `PreToolUse` hook polls for resolution only if `POST /api/v1/evaluate` still returns `decision=escalate`:

1. The hook calls `POST /api/v1/evaluate`, which may already return the final judge-mediated `approve` or `deny`
2. If the returned decision is still `escalate`, the hook receives a `call_id` and enters the polling loop
3. The hook checks `GET /api/v1/audit/{call_id}` with exponential backoff (2s, 4s, 8s, 16s, 30s cap)
4. If a human approves/denies in the Intaris UI, the hook returns immediately
5. If `INTARIS_ESCALATION_TIMEOUT` is reached, the hook denies. The deny includes both a `systemMessage` (surfaced directly in the terminal, so the wait is no longer silent) and a link into the Intaris UI for that session (`{INTARIS_URL}/?session_id=...`), which opens a modal showing the session's audit records

Built-in cross-project read defaults for the hooks are always present: `/tmp/*`, `/var/tmp/*`, `$TMPDIR/*` when `TMPDIR` is set, and `~/.claude/plans/*` when `HOME` is set — plus each of those directories' resolved physical path (e.g. `/private/tmp/*` on macOS) if it differs from the literal form. `INTARIS_ALLOW_PATHS` adds more prefixes on top of those defaults.

**Known limitation**: The PreToolUse hook timeout is `INTARIS_HOOK_TIMEOUT` (default 60s). Escalation approval must complete within roughly that window minus a 5s margin. The default `INTARIS_ESCALATION_TIMEOUT=55` leaves that margin at the default timeout. If approval takes longer, the user must retry the tool call after approving in the UI. On timeout the hook denies rather than falling back to `permissionDecision:"ask"` — under `permissions.defaultMode: "bypassPermissions"`, an "ask" hook result resolves to a silent **allow** before Claude Code ever surfaces a prompt, which would defeat the guardrail for exactly the sessions most likely to have that mode set.

## Sub-Agent Support

The hooks track Claude Code sub-agents (e.g., `Explore`, `Plan`, custom agents) as child sessions in Intaris:

- **`SubagentStart`**: Creates a child session with `parent_session_id` linking to the parent. The child session has its own intention, statistics, and evaluation context.
- **`SubagentStop`**: Signals child session completion with an agent summary.
- **`PreToolUse`** (inside sub-agents): Automatically detects the `agent_id` field and evaluates tool calls against the child session, not the parent.
- **Parent `Stop`**: Completes any remaining child sessions.

Child session IDs follow the format `cc-{session_id}--{agent_id}` (double-hyphen separator).

## Session Recording

When `INTARIS_SESSION_RECORDING=true`, the hooks record events to the Intaris event store for session playback and analysis.

### What's recorded

- **`UserPromptSubmit`**: Records `message` events with user prompt text
- **`PreToolUse`**: Records `tool_call` events with tool name, arguments, and evaluation decision
- **`PostToolUse`**: Records `tool_result` events with tool output
- **`Stop`**: Uploads the full Claude Code transcript (JSONL) as `transcript` events, then flushes

### How it works

Unlike the OpenCode plugin (which buffers events client-side and flushes on a size/interval timer), the Claude Code hooks send events directly on each invocation since each hook is a separate, short-lived bash process with nowhere to run a background timer. Each hook call sends 1-2 events via `POST /session/{id}/events`, backgrounded and disowned so the curl runs after the hook has already returned its decision. The server-side EventStore buffers and consolidates these into chunks.

Recording is completely non-blocking -- all recording API calls are backgrounded, fire-and-forget, with 2s timeouts. Recording failures never block tool execution, and (since they're backgrounded) never add their own latency to the hook either.

### Enable recording

```bash
export INTARIS_SESSION_RECORDING=true
```

## Behavioral Analysis

The hooks support Intaris's behavioral analysis pipeline:

- **User message forwarding**: Every user prompt is forwarded to `POST /api/v1/reasoning` with the assistant's last response as context. This enables the IntentionBarrier to track user intent and update session intentions dynamically.
- **Periodic checkpoints**: Every `INTARIS_CHECKPOINT_INTERVAL` evaluate calls, the hook sends a checkpoint with enriched content (call counts, decision breakdown, recent tool names). Checkpoints share the rate limit budget with evaluate calls.
- **Session completion**: The `Stop` hook signals session completion and sends an agent summary with session statistics (total calls, approve/deny/escalate breakdown, working directory).
- **Sub-agent tracking**: Sub-agent sessions are tracked as child sessions with their own statistics and summaries.
- **Session state tracking**: State is persisted as JSON files across hook invocations with file locking for concurrency safety.

## Tool Name Conventions

When using the **hooks** approach, tool names are passed as-is from Claude Code to Intaris. Claude Code uses these tool names:

- Built-in tools: `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`, `WebFetch`, `Task`
- MCP tools: `mcp__server__tool` format (e.g., `mcp__mnemory__add_memory`)

When configuring Intaris session policies (fnmatch patterns), use these names:

```json
{
  "policy": {
    "allow": ["Read", "Glob", "Grep"],
    "deny": ["Bash"]
  }
}
```

Note: Claude Code tool names are **capitalized** (e.g., `Bash` not `bash`). MCP tools use double underscores (e.g., `mcp__server__tool`).

When using the **MCP proxy** approach, tools are namespaced as `server_name:tool_name` (e.g., `mnemory:add_memory`).

## Session State Tracking

The hooks use JSON state files to track session state across hook invocations. State files are stored in `$TMPDIR` (per-user temp directory, falls back to `/tmp`). Each file contains:

```json
{
  "session_id": "cc-<session-id>",
  "call_count": 42,
  "approved": 38,
  "denied": 3,
  "escalated": 1,
  "recent_tools": ["Bash", "Edit", "Read"],
  "call_id_map": [
    {"tool_use_id": "toolu_abc123", "call_id": "9f2e..."}
  ],
  "cwd": "/path/to/project",
  "last_assistant_text": "I've completed the refactoring...",
  "subagents": {
    "agent-abc123": "cc-<session-id>--agent-abc123"
  }
}
```

`call_id_map` (last 10 entries) lets `PostToolUse` look up the exact Intaris `call_id` for a result by the tool call's `tool_use_id`, since Claude Code's `PostToolUse` payload never actually includes a `call_id` field.

State files are created by `SessionStart` and cleaned up by `SessionEnd`. Sub-agent state files follow the pattern `intaris_state_{session_id}_{agent_id}.json`.

### Concurrency

All state file operations use `mkdir`-based file locking (atomic on POSIX; a lock older than 30s is treated as abandoned and reclaimed, bounded to a handful of attempts so a genuinely stuck lock can't spin forever) and atomic writes (write to a unique `mktemp` file, then `mv`) to prevent corruption from concurrent hook invocations (e.g., parallel tool calls). Counters (call count, approve/deny/escalate breakdown, recent tools, the `call_id_map`) are recomputed from the state file's on-disk contents inside the same lock hold as the write, not from a snapshot read before the network call to Intaris, so concurrent invocations can't lose each other's increments.

### Cleanup

State files accumulate if Claude Code exits abnormally. Clean up manually:

```bash
rm ${TMPDIR:-/tmp}/intaris_state_*
```

The evaluate hook supports backward compatibility with legacy state files (plain session ID format) during upgrades.

## Architecture

```
hooks.json                    Hook configuration (9 hooks)
scripts/
  intaris-lib.sh              Shared library (logging, locking, headers, validation)
  intaris-session.sh          SessionStart handler (creates/re-activates session)
  intaris-prompt.sh           UserPromptSubmit handler (user message → /reasoning)
  intaris-evaluate.sh         PreToolUse handler (evaluate + escalation polling)
  intaris-record.sh           PostToolUse handler (session recording)
  intaris-subagent.sh         SubagentStart handler (child session creation)
  intaris-subagent-stop.sh    SubagentStop handler (child session completion)
  intaris-stop.sh             Stop handler (assistant text + session completion)
  intaris-stop-failure.sh     StopFailure handler (save assistant text on errors)
  intaris-session-end.sh      SessionEnd handler (local state cleanup)
```

## Troubleshooting

- **Tool calls blocked unexpectedly**: Check that `INTARIS_URL` and `INTARIS_API_KEY` are set. Enable `INTARIS_DEBUG=true` and check stderr for evaluation decisions.
- **Scripts not executing**: Ensure they're executable (`chmod +x ~/.claude/scripts/intaris-*.sh`). Check Claude Code logs for hook errors.
- **"Cannot create session" errors**: Verify Intaris is running and reachable at the configured URL.
- **All tool calls allowed**: If using `INTARIS_FAIL_OPEN=true`, Intaris may be unreachable. Check connectivity.
- **Slow first tool call**: If `SessionStart` didn't fire, the first `PreToolUse` creates a session (~1-2s) before evaluating.
- **Double evaluation**: If you see each tool call evaluated twice, you may have both hooks and MCP proxy configured. Use only one approach.
- **`jq` not found**: The scripts require `jq` for JSON processing. Install it: `brew install jq` (macOS) or `apt install jq` (Linux). Most hooks exit gracefully (`{}`, no-op) if `jq` is missing — except `PreToolUse`, which denies the tool call (exit 2, with a reason on stderr) rather than silently allowing it, since the fail-closed guarantee has to hold even when the JSON tooling it would normally use to express that isn't available.
- **Checkpoints not appearing**: Verify `INTARIS_CHECKPOINT_INTERVAL` is not set to `0`. Check that the rate limit is not exhausted (checkpoints share the budget with evaluate calls).
- **Stop or SessionEnd hook not firing**: If Claude Code crashes or is killed, either hook may not fire. The `StopFailure` hook handles API errors. The server's background sweep handles abnormal exits by transitioning idle sessions after `SESSION_IDLE_TIMEOUT_MINUTES`.
- **Escalation timeout**: If escalations always time out, check that the judge is configured (`JUDGE_MODE`) or that you can access the Intaris UI to approve/deny. The default 55s timeout can be adjusted via `INTARIS_ESCALATION_TIMEOUT`.
- **Sub-agent tool calls using wrong session**: If the first sub-agent tool call evaluates against the parent session, the `SubagentStart` hook may not have completed yet. The evaluate hook waits up to 2s for the child state file to appear.
- **Stale lock files**: If hooks hang, check for stale lock files: `ls ${TMPDIR:-/tmp}/intaris_state_*.lock.d`. Locks older than 30s are automatically cleaned up.
