#!/usr/bin/env bash
# intaris evaluate hook for Claude Code
#
# Called on PreToolUse. Evaluates the tool call through Intaris's safety
# pipeline and blocks denied or escalated calls. Tracks per-session
# statistics and sends periodic checkpoints.
#
# Input (JSON on stdin):
#   { "session_id": "...", "tool_name": "...", "tool_input": {...}, ... }
#
# Output (JSON on stdout):
#   {} = allow, {"decision": "block", "reason": "..."} = block
#
# Environment variables:
#   INTARIS_URL                  - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY              - API key for authentication (required)
#   INTARIS_AGENT_ID             - Agent ID (default: claude-code)
#   INTARIS_USER_ID              - User ID (optional if API key maps to user)
#   INTARIS_FAIL_OPEN            - Allow tool calls if Intaris is unreachable (default: false)
#   INTARIS_INTENTION            - Session intention override (default: auto-generated)
#   INTARIS_CHECKPOINT_INTERVAL  - Evaluate calls between checkpoints (default: 25, 0=disabled)
#   INTARIS_SESSION_RECORDING    - Enable session recording (default: false)
#   INTARIS_DEBUG                - Enable debug logging to stderr (default: false)

set -euo pipefail

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_FAIL_OPEN="${INTARIS_FAIL_OPEN:-false}"
INTARIS_INTENTION="${INTARIS_INTENTION:-}"
INTARIS_CHECKPOINT_INTERVAL="${INTARIS_CHECKPOINT_INTERVAL:-25}"
INTARIS_SESSION_RECORDING="${INTARIS_SESSION_RECORDING:-false}"
INTARIS_DEBUG="${INTARIS_DEBUG:-false}"

log() {
    if [ "$INTARIS_DEBUG" = "true" ]; then
        echo "[intaris] $*" >&2
    fi
}

# Read hook input from stdin
INPUT=$(cat)

# Extract fields from the hook input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)

if [ -z "$TOOL_NAME" ]; then
    log "No tool_name in hook input, allowing"
    echo '{}'
    exit 0
fi

# Build request headers
HEADERS=(-H "Content-Type: application/json" -H "X-Agent-Id: $INTARIS_AGENT_ID")
if [ -n "$INTARIS_API_KEY" ]; then
    HEADERS+=(-H "Authorization: Bearer $INTARIS_API_KEY")
fi
if [ -n "$INTARIS_USER_ID" ]; then
    HEADERS+=(-H "X-User-Id: $INTARIS_USER_ID")
fi

# -- Session State Management -----------------------------------------------
# Load or create Intaris session state from JSON state file.
# Supports both new JSON format and legacy plain-text format for
# backward compatibility during upgrades.

SESSION_FILE="/tmp/intaris_state_${SESSION_ID:-default}.json"
INTARIS_SESSION_ID=""
CALL_COUNT=0
APPROVED=0
DENIED=0
ESCALATED=0
RECENT_TOOLS="[]"

if [ -f "$SESSION_FILE" ]; then
    # Try JSON format first (new format from updated session.sh)
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
fi

# Lazy session creation if SessionStart hook didn't fire
if [ -z "$INTARIS_SESSION_ID" ]; then
    INTARIS_SESSION_ID="cc-${SESSION_ID:-$(date +%s)}"

    # Build intention
    if [ -n "$INTARIS_INTENTION" ]; then
        INTENTION="$INTARIS_INTENTION"
    elif [ -n "$CWD" ]; then
        INTENTION="Claude Code coding session in ${CWD}"
    else
        INTENTION="Claude Code coding session"
    fi

    INTENTION_BODY=$(jq -n \
        --arg session_id "$INTARIS_SESSION_ID" \
        --arg intention "$INTENTION" \
        --arg cwd "$CWD" \
        '{
            session_id: $session_id,
            intention: $intention,
            details: {
                source: "claude-code",
                working_directory: $cwd
            }
        }')

    log "Lazy session creation: $INTARIS_SESSION_ID"

    # 2s timeout for session creation
    curl -s --max-time 2 \
        -X POST \
        "${HEADERS[@]}" \
        -d "$INTENTION_BODY" \
        "${INTARIS_URL}/api/v1/intention" >/dev/null 2>&1 || true

    # Write initial JSON state file
    # NOTE: Keep JSON schema in sync with session.sh
    jq -n \
        --arg sid "$INTARIS_SESSION_ID" \
        --arg cwd "$CWD" \
        '{
            session_id: $sid,
            call_count: 0,
            approved: 0,
            denied: 0,
            escalated: 0,
            recent_tools: [],
            cwd: $cwd
        }' > "$SESSION_FILE"
    chmod 600 "$SESSION_FILE"
fi

# -- Evaluate ----------------------------------------------------------------

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

# 5s timeout for evaluation (within 10s hook timeout)
RESPONSE=$(curl -s --max-time 5 \
    -w "\n%{http_code}" \
    -X POST \
    "${HEADERS[@]}" \
    -d "$EVAL_BODY" \
    "${INTARIS_URL}/api/v1/evaluate" 2>/dev/null || echo -e "\n000")

# Split response body and HTTP status code
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

# Handle connection failures
if [ "$HTTP_CODE" = "000" ] || [ -z "$HTTP_CODE" ]; then
    log "Intaris unreachable"
    if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
        log "Allowing (fail-open)"
        echo '{}'
        exit 0
    fi
    jq -n '{decision: "block", reason: "[intaris] Evaluation failed — tool call blocked (INTARIS_FAIL_OPEN=false)"}'
    exit 0
fi

# Handle HTTP errors
if [ "$HTTP_CODE" != "200" ]; then
    DETAIL=$(echo "$BODY" | jq -r '.detail // "Unknown error"' 2>/dev/null || echo "HTTP $HTTP_CODE")
    log "Evaluate returned HTTP $HTTP_CODE: $DETAIL"
    if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
        log "Allowing (fail-open)"
        echo '{}'
        exit 0
    fi
    jq -n --arg reason "[intaris] Evaluation error: $DETAIL" '{decision: "block", reason: $reason}'
    exit 0
fi

# Parse the evaluation response
DECISION=$(echo "$BODY" | jq -r '.decision // "deny"' 2>/dev/null || echo "deny")
REASONING=$(echo "$BODY" | jq -r '.reasoning // "No reasoning provided"' 2>/dev/null || echo "")
CALL_ID=$(echo "$BODY" | jq -r '.call_id // ""' 2>/dev/null || echo "")
RISK=$(echo "$BODY" | jq -r '.risk // ""' 2>/dev/null || echo "")
PATH_TYPE=$(echo "$BODY" | jq -r '.path // ""' 2>/dev/null || echo "")
LATENCY=$(echo "$BODY" | jq -r '.latency_ms // 0' 2>/dev/null || echo "0")

log "$TOOL_NAME: $DECISION ($PATH_TYPE, ${LATENCY}ms, risk=$RISK)"

# -- Update Session State ---------------------------------------------------

CALL_COUNT=$((CALL_COUNT + 1))
case "$DECISION" in
    approve) APPROVED=$((APPROVED + 1)) ;;
    deny) DENIED=$((DENIED + 1)) ;;
    escalate) ESCALATED=$((ESCALATED + 1)) ;;
esac

# Update recent tools (keep last 10)
RECENT_TOOLS=$(echo "$RECENT_TOOLS" | jq --arg t "$TOOL_NAME" '(. + [$t])[-10:]' 2>/dev/null || echo "[]")

# Write updated state file
jq -n \
    --arg sid "$INTARIS_SESSION_ID" \
    --argjson cc "$CALL_COUNT" \
    --argjson ap "$APPROVED" \
    --argjson dn "$DENIED" \
    --argjson es "$ESCALATED" \
    --argjson rt "$RECENT_TOOLS" \
    --arg cwd "$CWD" \
    '{
        session_id: $sid,
        call_count: $cc,
        approved: $ap,
        denied: $dn,
        escalated: $es,
        recent_tools: $rt,
        cwd: $cwd
    }' > "$SESSION_FILE"
chmod 600 "$SESSION_FILE"

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

# -- Output Decision --------------------------------------------------------

case "$DECISION" in
    approve)
        echo '{}'
        ;;
    deny)
        jq -n --arg reason "[intaris] DENIED: $REASONING" \
            '{decision: "block", reason: $reason}'
        ;;
    escalate)
        jq -n --arg reason "[intaris] ESCALATED ($CALL_ID): $REASONING\nApprove or deny this call in the Intaris UI, then retry." \
            '{decision: "block", reason: $reason}'
        ;;
    *)
        log "Unknown decision: $DECISION"
        if [ "$INTARIS_FAIL_OPEN" = "true" ]; then
            echo '{}'
        else
            jq -n '{decision: "block", reason: "[intaris] Unknown evaluation decision — blocked"}'
        fi
        ;;
esac
