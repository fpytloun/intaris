#!/usr/bin/env bash
# intaris recording hook for Claude Code
#
# Called on PostToolUse. Records tool results to the Intaris event store
# for session recording and playback. No-op if INTARIS_SESSION_RECORDING
# is not set to "true".
#
# Input (JSON on stdin):
#   { "session_id": "...", "tool_name": "...", "tool_input": {...},
#     "tool_result": "...", ... }
#
# Output (JSON on stdout):
#   {} (always — recording never blocks)
#
# Environment variables:
#   INTARIS_URL                - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY            - API key for authentication (required)
#   INTARIS_AGENT_ID           - Agent ID (default: claude-code)
#   INTARIS_USER_ID            - User ID (optional if API key maps to user)
#   INTARIS_SESSION_RECORDING  - Enable session recording (default: false)
#   INTARIS_DEBUG              - Enable debug logging to stderr (default: false)

set -euo pipefail

INTARIS_SESSION_RECORDING="${INTARIS_SESSION_RECORDING:-false}"

# Quick exit if recording is disabled
if [ "$INTARIS_SESSION_RECORDING" != "true" ]; then
    echo '{}'
    exit 0
fi

# Guard: jq is required
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_DEBUG="${INTARIS_DEBUG:-false}"

log() {
    if [ "$INTARIS_DEBUG" = "true" ]; then
        echo "[intaris-record] $*" >&2
    fi
}

# Read hook input from stdin
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')
TOOL_RESULT=$(echo "$INPUT" | jq -c '.tool_result // null' 2>/dev/null || echo 'null')

if [ -z "$SESSION_ID" ] || [ -z "$TOOL_NAME" ]; then
    echo '{}'
    exit 0
fi

# Validate session ID format to prevent path traversal in state file paths
if [[ "$SESSION_ID" =~ [/\\] ]] || [[ "$SESSION_ID" == *".."* ]]; then
    echo '{}'
    exit 0
fi

# Load Intaris session ID from state file
SESSION_FILE="/tmp/intaris_state_${SESSION_ID}.json"
if [ ! -f "$SESSION_FILE" ]; then
    log "No state file, skipping recording"
    echo '{}'
    exit 0
fi

INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
if [ -z "$INTARIS_SESSION_ID" ]; then
    log "No session_id in state file, skipping recording"
    echo '{}'
    exit 0
fi

# Build request headers
HEADERS=(-H "Content-Type: application/json" -H "X-Agent-Id: $INTARIS_AGENT_ID" -H "X-Intaris-Source: claude-code")
if [ -n "$INTARIS_API_KEY" ]; then
    HEADERS+=(-H "Authorization: Bearer $INTARIS_API_KEY")
fi
if [ -n "$INTARIS_USER_ID" ]; then
    HEADERS+=(-H "X-User-Id: $INTARIS_USER_ID")
fi

# Build tool_result event
EVENT_BODY=$(jq -n \
    --arg tool "$TOOL_NAME" \
    --argjson args "$TOOL_INPUT" \
    --argjson result "$TOOL_RESULT" \
    '[{
        type: "tool_result",
        data: {
            tool: $tool,
            args: $args,
            result: $result
        }
    }]')

log "Recording tool_result for: $TOOL_NAME"

# Send event (fire-and-forget, 2s timeout)
curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$EVENT_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events" >/dev/null 2>&1 || true

# Output empty (recording never blocks)
echo '{}'
