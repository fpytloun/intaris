#!/usr/bin/env bash
# intaris evaluate hook for Claude Code
#
# Called on PreToolUse. Evaluates the tool call through Intaris's safety
# pipeline and blocks denied or escalated calls. Tracks per-session
# statistics and sends periodic checkpoints.
#
# Features:
#   - Retry with exponential backoff on transient failures
#   - Escalation polling (waits for judge/human approval)
#   - Session suspension polling (waits for reactivation)
#   - Session termination handling
#   - Subagent context (evaluates against child session)
#   - Periodic checkpoints with enriched statistics
#   - Session recording (tool_call events)
#
# Input (JSON on stdin):
#   { "session_id": "...", "tool_name": "...", "tool_input": {...}, ... }
#
# Output (JSON on stdout):
#   {} = allow
#   {"hookSpecificOutput": {"hookEventName": "PreToolUse",
#     "permissionDecision": "deny", "permissionDecisionReason": "..."}} = deny
#
# Environment variables:
#   INTARIS_URL                  - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY              - API key for authentication (required)
#   INTARIS_AGENT_ID             - Agent ID (default: claude-code)
#   INTARIS_USER_ID              - User ID (optional if API key maps to user)
#   INTARIS_FAIL_OPEN            - Allow tool calls if Intaris is unreachable (default: false)
#   INTARIS_INTENTION            - Session intention override (default: auto-generated)
#   INTARIS_ALLOW_PATHS          - Comma-separated parent directories to allow reads from
#   INTARIS_CHECKPOINT_INTERVAL  - Evaluate calls between checkpoints (default: 25, 0=disabled)
#   INTARIS_ESCALATION_TIMEOUT   - Max seconds to wait for escalation approval (default: 55)
#   INTARIS_SESSION_RECORDING    - Enable session recording (default: false)
#   INTARIS_DEBUG                - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

require_jq

# Record hook start time for timing budget
HOOK_START=$(date +%s)

# Read hook input from stdin
INPUT=$(cat)

# Extract fields from the hook input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
# Subagent context: agent_id is present when hook fires inside a subagent
HOOK_AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null || true)

if [ -z "$TOOL_NAME" ]; then
    log "No tool_name in hook input, allowing"
    allow_tool
    exit 0
fi

if [ -z "$SESSION_ID" ] || ! validate_session_id "$SESSION_ID"; then
    allow_tool
    exit 0
fi

build_headers

# -- Resolve Session ID (parent or subagent) ---------------------------------

SESSION_FILE=$(state_file_for "$SESSION_ID")
INTARIS_SESSION_ID=""
CALL_COUNT=0
APPROVED=0
DENIED=0
ESCALATED=0
RECENT_TOOLS="[]"

# Load state from a JSON state file into the global variables.
# Usage: load_state_from "path/to/state.json"
# Returns 0 if loaded successfully, 1 if file is missing or unreadable.
load_state_from() {
    local file="$1"
    [ ! -f "$file" ] && return 1
    acquire_lock "$file" || return 1
    INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$file" 2>/dev/null || true)
    CALL_COUNT=$(jq -r '.call_count // 0' "$file" 2>/dev/null || echo "0")
    APPROVED=$(jq -r '.approved // 0' "$file" 2>/dev/null || echo "0")
    DENIED=$(jq -r '.denied // 0' "$file" 2>/dev/null || echo "0")
    ESCALATED=$(jq -r '.escalated // 0' "$file" 2>/dev/null || echo "0")
    RECENT_TOOLS=$(jq -c '.recent_tools // []' "$file" 2>/dev/null || echo "[]")
    release_lock "$file"
    return 0
}

# Determine which state file and session ID to use
if [ -n "$HOOK_AGENT_ID" ]; then
    # Tool call inside a subagent — try to use child session
    CHILD_FILE=$(state_file_for_subagent "$SESSION_ID" "$HOOK_AGENT_ID")

    if load_state_from "$CHILD_FILE"; then
        SESSION_FILE="$CHILD_FILE"
    else
        # Child state file doesn't exist yet — SubagentStart may still be running.
        # Wait briefly for it to appear, then fall back to parent session.
        local_attempts=0
        while [ $local_attempts -lt 20 ] && [ ! -f "$CHILD_FILE" ]; do
            sleep 0.1
            local_attempts=$((local_attempts + 1))
        done

        if load_state_from "$CHILD_FILE"; then
            SESSION_FILE="$CHILD_FILE"
        else
            log "No child state file for agent $HOOK_AGENT_ID, using parent session"
        fi
    fi
fi

# Load parent state if we haven't loaded child state
if [ -z "$INTARIS_SESSION_ID" ] && [ -f "$SESSION_FILE" ]; then
    acquire_lock "$SESSION_FILE" || true
    # Try JSON format first (new format)
    if jq -e '.session_id' "$SESSION_FILE" >/dev/null 2>&1; then
        INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
        CALL_COUNT=$(jq -r '.call_count // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
        APPROVED=$(jq -r '.approved // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
        DENIED=$(jq -r '.denied // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
        ESCALATED=$(jq -r '.escalated // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
        RECENT_TOOLS=$(jq -c '.recent_tools // []' "$SESSION_FILE" 2>/dev/null || echo "[]")
    else
        # Legacy format: plain session ID or session_id:count
        LEGACY=$(cat "$SESSION_FILE" 2>/dev/null || true)
        IFS=':' read -r INTARIS_SESSION_ID CALL_COUNT <<< "$LEGACY"
        CALL_COUNT=${CALL_COUNT:-0}
    fi
    release_lock "$SESSION_FILE"
fi

# -- Lazy Session Creation ---------------------------------------------------

if [ -z "$INTARIS_SESSION_ID" ]; then
    INTARIS_SESSION_ID="cc-${SESSION_ID}"

    # Build intention
    if [ -n "$INTARIS_INTENTION" ]; then
        INTENTION="$INTARIS_INTENTION"
    elif [ -n "$CWD" ]; then
        INTENTION="Claude Code coding session in ${CWD}"
    else
        INTENTION="Claude Code coding session"
    fi

    # Build allow_paths policy (was missing in original lazy creation)
    POLICY_JSON=$(build_allow_paths_policy)

    INTENTION_BODY=$(jq -n \
        --arg session_id "$INTARIS_SESSION_ID" \
        --arg intention "$INTENTION" \
        --arg cwd "$CWD" \
        --argjson policy "$POLICY_JSON" \
        '{
            session_id: $session_id,
            intention: $intention,
            details: {
                source: "claude-code",
                working_directory: $cwd
            }
        } + (if $policy != null then {policy: $policy} else {} end)')

    log "Lazy session creation: $INTARIS_SESSION_ID"

    curl -s --max-time 2 \
        -X POST \
        "${HEADERS[@]}" \
        -d "$INTENTION_BODY" \
        "${INTARIS_URL}/api/v1/intention" >/dev/null 2>&1 || true

    # Write initial state file
    STATE_JSON=$(jq -n \
        --arg sid "$INTARIS_SESSION_ID" \
        --arg cwd "$CWD" \
        '{
            session_id: $sid,
            call_count: 0,
            approved: 0,
            denied: 0,
            escalated: 0,
            recent_tools: [],
            cwd: $cwd,
            last_assistant_text: "",
            subagents: {}
        }')

    acquire_lock "$SESSION_FILE" || true
    write_state "$SESSION_FILE" "$STATE_JSON"
    release_lock "$SESSION_FILE"
fi

# -- Evaluate with Retry -----------------------------------------------------

log "Evaluating: $TOOL_NAME"

EVAL_BODY=$(jq -n \
    --arg session_id "$INTARIS_SESSION_ID" \
    --arg tool "$TOOL_NAME" \
    --argjson args "$TOOL_INPUT" \
    '{
        session_id: $session_id,
        tool: $tool,
        args: $args
    }')

# Retry with exponential backoff: 3 attempts, 1s/2s/4s delays
# Only retry on network errors (000) and 5xx. Do NOT retry 4xx.
BACKOFF_DELAYS=(1 2 4)
MAX_ATTEMPTS=3
BODY=""
HTTP_CODE="000"

for attempt in $(seq 0 $((MAX_ATTEMPTS - 1))); do
    RESPONSE=$(curl -s --max-time 8 \
        -w "\n%{http_code}" \
        -X POST \
        "${HEADERS[@]}" \
        -d "$EVAL_BODY" \
        "${INTARIS_URL}/api/v1/evaluate" 2>/dev/null || printf '\n000')

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    # Success
    if [ "$HTTP_CODE" = "200" ]; then
        break
    fi

    # 4xx client error — do not retry
    if [ "$HTTP_CODE" != "000" ] && [ "$HTTP_CODE" -ge 400 ] 2>/dev/null && [ "$HTTP_CODE" -lt 500 ] 2>/dev/null; then
        break
    fi

    # 5xx or network error — retry with backoff (unless last attempt)
    if [ $attempt -lt $((MAX_ATTEMPTS - 1)) ]; then
        local_delay=${BACKOFF_DELAYS[$attempt]}
        log "Evaluate failed (HTTP $HTTP_CODE, attempt $((attempt + 1))/$MAX_ATTEMPTS), retrying in ${local_delay}s"
        sleep "$local_delay"
    fi
done

# -- Handle Connection Failures ----------------------------------------------

if [ "$HTTP_CODE" = "000" ] || [ -z "$HTTP_CODE" ]; then
    log "Intaris unreachable after $MAX_ATTEMPTS attempts"
    if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
        log "Allowing (fail-open)"
        allow_tool
        exit 0
    fi
    deny_tool "[intaris] Evaluation failed — tool call blocked (INTARIS_FAIL_OPEN=false)"
    exit 0
fi

# -- Handle HTTP Errors ------------------------------------------------------

if [ "$HTTP_CODE" != "200" ]; then
    DETAIL=$(echo "$BODY" | jq -r '.detail // "Unknown error"' 2>/dev/null || echo "HTTP $HTTP_CODE")
    log "Evaluate returned HTTP $HTTP_CODE: $DETAIL"
    if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
        log "Allowing (fail-open)"
        allow_tool
        exit 0
    fi
    deny_tool "[intaris] Evaluation error: $DETAIL"
    exit 0
fi

# -- Parse Evaluation Response -----------------------------------------------

DECISION=$(echo "$BODY" | jq -r '.decision // "deny"' 2>/dev/null || echo "deny")
REASONING=$(echo "$BODY" | jq -r '.reasoning // "No reasoning provided"' 2>/dev/null || echo "")
CALL_ID=$(echo "$BODY" | jq -r '.call_id // ""' 2>/dev/null || echo "")
RISK=$(echo "$BODY" | jq -r '.risk // ""' 2>/dev/null || echo "")
PATH_TYPE=$(echo "$BODY" | jq -r '.path // ""' 2>/dev/null || echo "")
LATENCY=$(echo "$BODY" | jq -r '.latency_ms // 0' 2>/dev/null || echo "0")
SESSION_STATUS=$(echo "$BODY" | jq -r '.session_status // ""' 2>/dev/null || echo "")
STATUS_REASON=$(echo "$BODY" | jq -r '.status_reason // ""' 2>/dev/null || echo "")

log "$TOOL_NAME: $DECISION ($PATH_TYPE, ${LATENCY}ms, risk=$RISK)"

# -- Update Session State ----------------------------------------------------

CALL_COUNT=$((CALL_COUNT + 1))
case "$DECISION" in
    approve) APPROVED=$((APPROVED + 1)) ;;
    deny) DENIED=$((DENIED + 1)) ;;
    escalate) ESCALATED=$((ESCALATED + 1)) ;;
esac

# Update recent tools (keep last 10)
RECENT_TOOLS=$(echo "$RECENT_TOOLS" | jq --arg t "$TOOL_NAME" '(. + [$t])[-10:]' 2>/dev/null || echo "[]")

# Write updated state (preserve existing fields like last_assistant_text, subagents)
acquire_lock "$SESSION_FILE" || true
if [ -f "$SESSION_FILE" ]; then
    UPDATED_STATE=$(jq \
        --argjson cc "$CALL_COUNT" \
        --argjson ap "$APPROVED" \
        --argjson dn "$DENIED" \
        --argjson es "$ESCALATED" \
        --argjson rt "$RECENT_TOOLS" \
        '.call_count = $cc | .approved = $ap | .denied = $dn | .escalated = $es | .recent_tools = $rt' \
        "$SESSION_FILE" 2>/dev/null)
    if [ -n "$UPDATED_STATE" ]; then
        write_state "$SESSION_FILE" "$UPDATED_STATE"
    fi
else
    # State file disappeared — recreate
    write_state "$SESSION_FILE" "$(jq -n \
        --arg sid "$INTARIS_SESSION_ID" \
        --argjson cc "$CALL_COUNT" \
        --argjson ap "$APPROVED" \
        --argjson dn "$DENIED" \
        --argjson es "$ESCALATED" \
        --argjson rt "$RECENT_TOOLS" \
        --arg cwd "$CWD" \
        '{session_id: $sid, call_count: $cc, approved: $ap, denied: $dn, escalated: $es, recent_tools: $rt, cwd: $cwd, last_assistant_text: "", subagents: {}}')"
fi
release_lock "$SESSION_FILE"

# -- Periodic Checkpoint (fire-and-forget) -----------------------------------

if [ "$INTARIS_CHECKPOINT_INTERVAL" -gt 0 ] 2>/dev/null && [ $((CALL_COUNT % INTARIS_CHECKPOINT_INTERVAL)) -eq 0 ]; then
    CHECKPOINT_NUM=$((CALL_COUNT / INTARIS_CHECKPOINT_INTERVAL))
    TOOLS_LIST=$(echo "$RECENT_TOOLS" | jq -r 'join(", ")' 2>/dev/null || echo "unknown")
    CHECKPOINT_CONTENT="Checkpoint #${CHECKPOINT_NUM}: ${CALL_COUNT} calls (${APPROVED} approved, ${DENIED} denied, ${ESCALATED} escalated). Recent tools: ${TOOLS_LIST}"

    CHECKPOINT_BODY=$(jq -n \
        --arg sid "$INTARIS_SESSION_ID" \
        --arg content "$CHECKPOINT_CONTENT" \
        '{session_id: $sid, content: $content}')

    log "Sending checkpoint #${CHECKPOINT_NUM}"
    curl -s --max-time 2 \
        -X POST \
        "${HEADERS[@]}" \
        -d "$CHECKPOINT_BODY" \
        "${INTARIS_URL}/api/v1/checkpoint" >/dev/null 2>&1 || true
fi

# -- Session Recording (fire-and-forget) ------------------------------------

if [ "$INTARIS_SESSION_RECORDING" = "true" ]; then
    RECORD_BODY=$(jq -n \
        --arg tool "$TOOL_NAME" \
        --argjson args "$TOOL_INPUT" \
        --arg decision "$DECISION" \
        --arg risk "$RISK" \
        --arg call_id "$CALL_ID" \
        '[{
            type: "tool_call",
            data: {
                tool: $tool,
                args: $args,
                decision: $decision,
                risk: $risk,
                call_id: $call_id
            }
        }]')

    curl -s --max-time 2 \
        -X POST \
        "${HEADERS[@]}" \
        -H "X-Intaris-Source: claude-code" \
        -d "$RECORD_BODY" \
        "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events" >/dev/null 2>&1 || true
fi

# -- Helper: Check Timing Budget --------------------------------------------

# Returns 0 if we still have time, 1 if we should exit.
# This is the outer safety ceiling (55s from hook entry) that prevents the
# hook from being killed by Claude Code's 60s timeout. The user-configured
# INTARIS_ESCALATION_TIMEOUT is checked separately inside each polling loop.
check_timing_budget() {
    local now
    now=$(date +%s)
    local elapsed=$((now - HOOK_START))
    if [ $elapsed -ge 55 ]; then
        return 1
    fi
    return 0
}

# -- Handle Session Suspension -----------------------------------------------

handle_suspension() {
    local status_reason="$1"
    log "Session suspended: $status_reason. Polling for reactivation..."

    local poll_backoff=(2 4 8 16 30)
    local poll_attempt=0

    while check_timing_budget; do
        # Check escalation timeout
        if [ "$INTARIS_ESCALATION_TIMEOUT" -gt 0 ] 2>/dev/null; then
            local elapsed=$(($(date +%s) - HOOK_START))
            if [ $elapsed -ge "$INTARIS_ESCALATION_TIMEOUT" ]; then
                deny_tool "[intaris] Session suspension timeout: $status_reason. Reactivate or terminate in the Intaris UI."
                exit 0
            fi
        fi

        local delay=${poll_backoff[$poll_attempt]}
        [ -z "$delay" ] && delay=30
        sleep "$delay"
        poll_attempt=$((poll_attempt + 1))

        # Poll session status
        local session_resp
        session_resp=$(curl -s --max-time 5 \
            "${HEADERS[@]}" \
            "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}" 2>/dev/null || echo '{}')

        local current_status
        current_status=$(echo "$session_resp" | jq -r '.status // ""' 2>/dev/null || echo "")

        if [ "$current_status" = "active" ]; then
            log "Session reactivated — re-evaluating $TOOL_NAME"
            # Re-evaluate the tool call
            local re_resp
            re_resp=$(curl -s --max-time 8 \
                -w "\n%{http_code}" \
                -X POST \
                "${HEADERS[@]}" \
                -d "$EVAL_BODY" \
                "${INTARIS_URL}/api/v1/evaluate" 2>/dev/null || printf '\n000')

            local re_code
            re_code=$(echo "$re_resp" | tail -1)
            local re_body
            re_body=$(echo "$re_resp" | sed '$d')

            if [ "$re_code" != "200" ]; then
                if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
                    allow_tool
                else
                    deny_tool "[intaris] Re-evaluation failed after session reactivation"
                fi
                exit 0
            fi

            local re_decision
            re_decision=$(echo "$re_body" | jq -r '.decision // "deny"' 2>/dev/null || echo "deny")
            local re_reasoning
            re_reasoning=$(echo "$re_body" | jq -r '.reasoning // ""' 2>/dev/null || echo "")

            if [ "$re_decision" = "approve" ]; then
                allow_tool
                exit 0
            fi
            # Treat escalate-after-reactivation as deny to prevent infinite
            # polling loops. The user just reactivated the session — they can
            # retry the tool call, which will create a fresh escalation.
            deny_tool "[intaris] DENIED after reactivation: ${re_reasoning:-Tool call denied}"
            exit 0
        fi

        if [ "$current_status" = "terminated" ]; then
            local term_reason
            term_reason=$(echo "$session_resp" | jq -r '.status_reason // "terminated by user"' 2>/dev/null || echo "terminated by user")
            deny_tool "[intaris] Session terminated: $term_reason"
            exit 0
        fi

        # Still suspended — continue polling
    done

    # Timing budget exhausted
    deny_tool "[intaris] Session suspension timeout (hook limit reached): $status_reason. Reactivate in the Intaris UI."
    exit 0
}

# -- Handle Escalation -------------------------------------------------------

handle_escalation() {
    local call_id="$1"
    local reasoning="$2"

    log "Escalated: $TOOL_NAME ($call_id). Polling for approval..."

    local poll_backoff=(2 4 8 16 30)
    local poll_attempt=0

    while check_timing_budget; do
        # Check escalation timeout
        if [ "$INTARIS_ESCALATION_TIMEOUT" -gt 0 ] 2>/dev/null; then
            local elapsed=$(($(date +%s) - HOOK_START))
            if [ $elapsed -ge "$INTARIS_ESCALATION_TIMEOUT" ]; then
                deny_tool "[intaris] ESCALATION TIMEOUT ($call_id): $reasoning. Approve or deny in the Intaris UI, then retry."
                exit 0
            fi
        fi

        local delay=${poll_backoff[$poll_attempt]}
        [ -z "$delay" ] && delay=30
        sleep "$delay"
        poll_attempt=$((poll_attempt + 1))

        # Poll audit record for resolution
        local audit_resp
        audit_resp=$(curl -s --max-time 5 \
            "${HEADERS[@]}" \
            "${INTARIS_URL}/api/v1/audit/${call_id}" 2>/dev/null || echo '{}')

        local user_decision
        user_decision=$(echo "$audit_resp" | jq -r '.user_decision // ""' 2>/dev/null || echo "")

        if [ "$user_decision" = "approve" ]; then
            log "Escalation approved: $TOOL_NAME ($call_id)"
            allow_tool
            exit 0
        fi

        if [ "$user_decision" = "deny" ]; then
            local user_note
            user_note=$(echo "$audit_resp" | jq -r '.user_note // ""' 2>/dev/null || echo "")
            local deny_suffix=""
            [ -n "$user_note" ] && deny_suffix=" — $user_note"
            log "Escalation denied: $TOOL_NAME ($call_id)"
            deny_tool "[intaris] DENIED by reviewer ($call_id): ${reasoning}${deny_suffix}"
            exit 0
        fi

        # No decision yet — continue polling
    done

    # Timing budget exhausted
    deny_tool "[intaris] ESCALATED ($call_id): $reasoning. Approve or deny in the Intaris UI, then retry."
    exit 0
}

# -- Output Decision ---------------------------------------------------------

case "$DECISION" in
    approve)
        allow_tool
        ;;
    deny)
        # Handle session-level suspension
        if [ "$SESSION_STATUS" = "suspended" ]; then
            handle_suspension "${STATUS_REASON:-Session suspended}"
        fi

        # Handle session termination
        if [ "$SESSION_STATUS" = "terminated" ]; then
            deny_tool "[intaris] Session terminated: ${STATUS_REASON:-terminated by user}"
            exit 0
        fi

        deny_tool "[intaris] DENIED: $REASONING"
        ;;
    escalate)
        handle_escalation "$CALL_ID" "$REASONING"
        ;;
    *)
        log "Unknown decision: $DECISION"
        if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
            allow_tool
        else
            deny_tool "[intaris] Unknown evaluation decision — blocked"
        fi
        ;;
esac
