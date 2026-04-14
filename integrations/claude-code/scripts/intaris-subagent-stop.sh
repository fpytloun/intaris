#!/usr/bin/env bash
# intaris subagent stop hook for Claude Code
#
# Called on SubagentStop. Signals child session completion to Intaris and
# sends an agent summary with session statistics. Cleans up child state
# file and removes the mapping from the parent state file.
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

if ! require_jq; then
    log "jq is required for SubagentStop, skipping"
    echo '{}'
    exit 0
fi

# Read hook input from stdin
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null || true)
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ] || [ -z "$AGENT_ID" ]; then
    log "Missing session_id or agent_id in SubagentStop input, skipping"
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Validate agent_id format (same rules as session_id — no path traversal)
if ! validate_session_id "$AGENT_ID"; then
    echo '{}'
    exit 0
fi

# Load child state file
CHILD_FILE=$(state_file_for_subagent "$SESSION_ID" "$AGENT_ID")

if [ ! -f "$CHILD_FILE" ]; then
    log "No child state file for agent $AGENT_ID, skipping"
    echo '{}'
    exit 0
fi

acquire_lock "$CHILD_FILE" || { echo '{}'; exit 0; }

CHILD_SESSION_ID=$(jq -r '.session_id // empty' "$CHILD_FILE" 2>/dev/null || true)
CALL_COUNT=$(jq -r '.call_count // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
APPROVED=$(jq -r '.approved // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
DENIED=$(jq -r '.denied // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
ESCALATED=$(jq -r '.escalated // 0' "$CHILD_FILE" 2>/dev/null || echo "0")
CHILD_CWD=$(jq -r '.cwd // empty' "$CHILD_FILE" 2>/dev/null || true)

release_lock "$CHILD_FILE"

if [ -z "$CHILD_SESSION_ID" ]; then
    log "No session_id in child state file, skipping"
    echo '{}'
    exit 0
fi

build_headers

# Build agent summary
AGENT_LABEL="${AGENT_TYPE:-sub-agent}"
SUMMARY="Claude Code ${AGENT_LABEL} session completed. ${CALL_COUNT} tool calls (${APPROVED} approved, ${DENIED} denied, ${ESCALATED} escalated)."
if [ -n "$CHILD_CWD" ]; then
    SUMMARY="${SUMMARY} Working directory: ${CHILD_CWD}"
fi

log "Signaling completion for child session: $CHILD_SESSION_ID (agent: $AGENT_ID)"

# Send status update and agent summary in parallel (fire-and-forget)
curl -s --max-time 2 \
    -X PATCH \
    "${HEADERS[@]}" \
    -d '{"status":"completed"}' \
    "${INTARIS_URL}/api/v1/session/${CHILD_SESSION_ID}/status" >/dev/null 2>&1 &

curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$(jq -n --arg s "$SUMMARY" '{summary: $s}')" \
    "${INTARIS_URL}/api/v1/session/${CHILD_SESSION_ID}/agent-summary" >/dev/null 2>&1 &

wait

# Clean up child state file
rm -f "$CHILD_FILE"

# Remove mapping from parent state file
PARENT_FILE=$(state_file_for "$SESSION_ID")
if [ -f "$PARENT_FILE" ]; then
    acquire_lock "$PARENT_FILE" || { echo '{}'; exit 0; }
    if [ -f "$PARENT_FILE" ]; then
        UPDATED=$(jq --arg aid "$AGENT_ID" 'del(.subagents[$aid])' "$PARENT_FILE" 2>/dev/null || cat "$PARENT_FILE")
        write_state "$PARENT_FILE" "$UPDATED"
    fi
    release_lock "$PARENT_FILE"
fi

log "Child session completed: $CHILD_SESSION_ID"

# Output empty
echo '{}'
