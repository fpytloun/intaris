# Evaluation Pipeline

How Intaris classifies, evaluates, and decides on every tool call.

## Overview

Every tool call passes through this pipeline:

```
Tool Call
    │
    ▼
┌──────────────────┐
│  Session Status  │──── suspended/terminated/completed → DENY (fast)
│     Check        │
└────────┬─────────┘
         │ active/idle
         ▼
┌──────────────────┐
│   Rate Limiter   │──── exceeded → HTTP 429
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Intention &     │──── waits up to 1s for pending intention update
│  Alignment       │──── waits up to 15s for pending alignment check
│  Barriers        │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌─────────────────┐
│   Classifier     │────►│ READ → APPROVE  │ (fast path, <1ms)
│                  │     │ CRITICAL → DENY │ (fast path)
│                  │     │ ESCALATE → ESC. │ (fast path)
└────────┬─────────┘     └─────────────────┘
         │ WRITE
         ▼
┌──────────────────┐
│  LLM Safety      │──── structured output: aligned, risk, decision
│  Evaluation      │──── timeout: 4000ms default
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Decision Matrix │──── priority-ordered rules → approve/deny/escalate
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   Audit Log      │──── record decision, reasoning, redacted args
│   + Webhook      │──── notify on escalation (if configured)
│   + EventBus     │──── real-time streaming to UI
└──────────────────┘
```

## Classification

The classifier determines whether a tool call needs LLM evaluation or can be fast-pathed.

### Classification Priority Chain

Checked in order -- first match wins:

| Step | Check | Result | Description |
|---|---|---|---|
| 1 | Session policy `deny_tools` / `deny_commands` | **CRITICAL** | Admin-defined deny rules |
| 1.5 | Session policy `deny_paths` | **CRITICAL** | Filesystem path deny (fnmatch) |
| 2 | Tool preference deny | **CRITICAL** | Per-tool MCP preference |
| 3 | Tool preference escalate | **ESCALATE** | Per-tool MCP preference |
| 4 | Session policy `allow_tools` / `allow_commands` | **READ** | Admin-defined allow rules |
| 5 | Tool preference auto-approve | **READ** | Per-tool MCP preference |
| 6 | Critical pattern detection (bash) | **CRITICAL** | Dangerous commands (rm -rf, etc.) |
| 7 | Built-in read-only allowlist | **READ** | Known safe tools and commands |
| 7.5 | Path outside project boundary | **WRITE** | Forces LLM evaluation for out-of-project reads |
| 8 | Default | **WRITE** | Everything else goes through LLM |

### Read-Only Allowlist

Tools that are always classified as READ (auto-approved):

**Built-in tools:** `read`, `glob`, `grep`, `search`, `find`, `list`, `get`, `view`, `show`, `describe`, `explain`, `help`, `info`, `status`, `whoami`, `version`, `todoread`, `todowrite`

**Bash commands:** `ls`, `cat`, `head`, `tail`, `find`, `tree`, `wc`, `grep`, `rg`, `fd`, `pwd`, `echo`, `which`, `file`, `stat`, `du`, `df`, `env`, `printenv`, `uname`, `hostname`, `whoami`, `id`, `date`, `uptime`, `diff`, `sort`, `uniq`, `cut`, `jq`, `yq`, and more.

**Git subcommands:** `status`, `log`, `diff`, `show`, `branch`, `tag`, `remote`, `stash list`, `config --get`, `rev-parse`, `ls-files`, `ls-tree`, `shortlog`, `describe`, `blame`, `reflog`

### Critical Patterns

Bash commands that are always classified as CRITICAL (auto-denied):

- `rm -rf /` and variants
- `chmod 777`
- `curl | sh`, `wget | sh` (pipe to shell)
- `dd if=` (raw disk operations)
- `mkfs`, `fdisk`, `parted` (disk formatting)
- `iptables`, `ufw` (firewall changes)
- `shutdown`, `reboot`, `halt`, `poweroff`
- `kill -9`, `killall`
- Environment variable manipulation (`export`, `unset` of sensitive vars)

### Filesystem Path Protection

When `working_directory` is set on a session, the classifier enforces filesystem boundaries:

1. **Path extraction**: File paths are extracted from tool arguments using known keys (`filePath`, `file_path`, `path`, `directory`, etc.). Relative paths are resolved against `working_directory`.

2. **Deny paths** (step 1.5): Paths matching `deny_paths` patterns are classified as CRITICAL. Always checked, even when the approved paths cache has entries.

3. **Out-of-project override** (step 7.5): Read-only tools targeting paths outside the project directory are reclassified as WRITE, forcing LLM evaluation.

4. **Allow paths**: Paths matching `allow_paths` patterns are exempt from the out-of-project override.

5. **Approved path prefix cache**: When the LLM approves an out-of-project read, the evaluator caches the approved directory prefix. Subsequent reads under that prefix are fast-pathed without LLM evaluation. Max 50 prefixes per session, FIFO eviction.

## LLM Safety Evaluation

Tool calls classified as WRITE are sent to the LLM for safety evaluation.

### Evaluation Context

The LLM receives:

- **Tool name and arguments** (redacted)
- **Session intention** (what the user is trying to accomplish)
- **Parent intention** (if child session, for defense-in-depth)
- **Project path** (if `working_directory` is set)
- **Behavioral alert** (if the user has a high/critical risk profile)
- **Recent tool history** (last few tool calls for context)

### Structured Output

The LLM returns a JSON object:

```json
{
  "aligned": true,
  "risk": "low",
  "reasoning": "The grep command searches for test patterns in the test directory, directly supporting the stated intention.",
  "decision": "approve"
}
```

Fields:
- **aligned** (boolean): Whether the tool call aligns with the session intention
- **risk** (`low` | `medium` | `high` | `critical`): Security risk level
- **reasoning** (string): Explanation of the assessment
- **decision** (`approve` | `deny` | `escalate`): LLM's recommended decision

### Timeout

Default: 4000ms (`LLM_TIMEOUT_MS`). Must be under the 5-second circuit breaker in client integrations. If the LLM times out, the evaluation fails and the tool call is blocked (fail-closed).

## Decision Matrix

The decision matrix applies priority-ordered rules to the LLM evaluation result:

| Priority | Condition | Decision |
|---|---|---|
| 1 | Critical risk (any alignment) | **Deny** -- auto-denied regardless of alignment |
| 2 | LLM explicitly said "deny" | **Deny** -- LLM override |
| 3 | Aligned + low risk | **Approve** |
| 4 | Aligned + medium risk | **Approve** |
| 5 | Aligned + high risk | **Escalate** -- requires human review |
| 6 | Not aligned (any risk) | **Escalate** -- requires human review |

### Evaluation Paths

Each decision is tagged with the path that produced it:

| Path | Description |
|---|---|
| `fast` | Read-only auto-approve, escalation retry, or session status deny |
| `critical` | Critical pattern auto-deny |
| `llm` | LLM safety evaluation -> decision matrix |
| `alignment` | Alignment misalignment escalation (child vs parent intention) |

## Escalation Workflow

When a tool call is escalated:

1. **Audit record** created with `decision=escalate`
2. **Webhook** fires (if configured) to notify external systems
3. **Notification** sent to user's configured channels (Pushover, Slack, webhook)
4. **Client** blocks the tool call and directs the user to the Intaris UI
5. **User** reviews the escalation in the Approvals tab and approves or denies
6. **Resolution** recorded via `POST /api/v1/decision`

### Escalation Retry

When a tool call is escalated and later approved, subsequent identical calls (same tool + same args) reuse the approval for 10 minutes. Identity is based on SHA-256 of the normalized arguments JSON.

### Standalone Mode

Without a webhook configured, escalations are denied by default with a message directing the user to the Intaris UI. With the management UI, users can still approve escalations manually.

## Rate Limiting

Per-session sliding window rate limiter. Tracks call timestamps per `(user_id, session_id)` pair. Configured via `RATE_LIMIT` (default 60 calls/minute, 0 = disabled). Returns HTTP 429 when exceeded.

The rate limit check runs before classification and LLM evaluation.

## Secret Redaction

All tool arguments are redacted before audit storage. The redactor catches:

- **Pattern-based**: API keys, passwords, connection strings, JWTs, private keys, AWS credentials
- **Key-name-based**: Any argument key containing `password`, `token`, `secret`, `key`, `credential`, `auth`

Redaction always returns a deep copy -- input arguments are never mutated.
