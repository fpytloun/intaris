#!/usr/bin/env bash
# intaris stop hook for Claude Code
#
# Called on Stop. Signals session completion to Intaris and sends an
# agent summary with session statistics. Both HTTP calls run in parallel
# to stay within the hook timeout.
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

# Guard: jq is required for JSON state file parsing
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_DEBUG="${INTARIS_DEBUG:-false}"

log() {
    if [ "$INTARIS_DEBUG" = "true" ]; then
        echo "[intaris] $*" >&2
    fi
}

# Read hook input from stdin
INPUT=$(cat)

# Extract session info from the hook input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ]; then
    log "No session_id in hook input, skipping"
    echo '{}'
    exit 0
fi

# Load session state from JSON state file
SESSION_FILE="/tmp/intaris_state_${SESSION_ID}.json"

if [ ! -f "$SESSION_FILE" ]; then
    log "No state file found, skipping"
    echo '{}'
    exit 0
fi

# Parse state file (JSON format)
INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)

if [ -z "$INTARIS_SESSION_ID" ]; then
    log "No session_id in state file, skipping"
    echo '{}'
    exit 0
fi

CALL_COUNT=$(jq -r '.call_count // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
APPROVED=$(jq -r '.approved // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
DENIED=$(jq -r '.denied // 0' "$SESSION_FILE" 2>/dev/null || echo "0")
ESCALATED=$(jq -r '.escalated // 0' "$SESSION_FILE" 2>/dev/null || echo "0")

# Fall back to state file cwd if not in hook input
if [ -z "$CWD" ]; then
    CWD=$(jq -r '.cwd // empty' "$SESSION_FILE" 2>/dev/null || true)
fi

# Build request headers
HEADERS=(-H "Content-Type: application/json" -H "X-Agent-Id: $INTARIS_AGENT_ID")
if [ -n "$INTARIS_API_KEY" ]; then
    HEADERS+=(-H "Authorization: Bearer $INTARIS_API_KEY")
fi
if [ -n "$INTARIS_USER_ID" ]; then
    HEADERS+=(-H "X-User-Id: $INTARIS_USER_ID")
fi

# Build agent summary
SUMMARY="Claude Code session completed. ${CALL_COUNT} tool calls (${APPROVED} approved, ${DENIED} denied, ${ESCALATED} escalated)."
if [ -n "$CWD" ]; then
    SUMMARY="${SUMMARY} Working directory: ${CWD}"
fi

# Build request bodies
STATUS_BODY=$(jq -n '{status: "completed"}')
SUMMARY_BODY=$(jq -n --arg s "$SUMMARY" '{summary: $s}')

log "Signaling completion for session: $INTARIS_SESSION_ID"

# Send status update and agent summary in parallel (fire-and-forget)
curl -s --max-time 2 \
    -X PATCH \
    "${HEADERS[@]}" \
    -d "$STATUS_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/status" >/dev/null 2>&1 &

curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$SUMMARY_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/agent-summary" >/dev/null 2>&1 &

# Wait for both background calls to complete (or timeout)
wait

log "Session completion signaled: $INTARIS_SESSION_ID"

# Clean up state file
rm -f "$SESSION_FILE"

# Output empty (no modifications to Claude's behavior)
echo '{}'
