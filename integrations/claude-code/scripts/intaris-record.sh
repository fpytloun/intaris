#!/usr/bin/env bash
# intaris recording hook for Claude Code
#
# Called on PostToolUse. Records tool results to the Intaris event store
# for session recording and playback. No-op if INTARIS_SESSION_RECORDING
# is not set to "true".
#
# Input (JSON on stdin):
#   { "session_id": "...", "tool_name": "...", "tool_input": {...},
#     "tool_response": {...}, ... }
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

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

# Quick exit if recording is disabled
if [ "$INTARIS_SESSION_RECORDING" != "true" ]; then
    echo '{}'
    exit 0
fi

if ! require_jq; then
    log "jq is required for PostToolUse recording, skipping"
    echo '{}'
    exit 0
fi

# Read hook input from stdin
INPUT=$(cat)

# Extract fields
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}' 2>/dev/null || echo '{}')
TOOL_RESPONSE=$(echo "$INPUT" | jq -c '.tool_response // null' 2>/dev/null || echo 'null')
CALL_ID=$(echo "$INPUT" | jq -r '.call_id // empty' 2>/dev/null || true)
TOOL_USE_ID=$(echo "$INPUT" | jq -r '.tool_use_id // empty' 2>/dev/null || true)
DURATION_MS=$(echo "$INPUT" | jq -r '.duration_ms // empty' 2>/dev/null || true)
# Subagent context
HOOK_AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ] || [ -z "$TOOL_NAME" ]; then
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Resolve Intaris session ID (child or parent)
INTARIS_SESSION_ID=""
SESSION_FILE=""

if [ -n "$HOOK_AGENT_ID" ]; then
    CHILD_FILE=$(state_file_for_subagent "$SESSION_ID" "$HOOK_AGENT_ID")
    if [ -f "$CHILD_FILE" ]; then
        SESSION_FILE="$CHILD_FILE"
        INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$CHILD_FILE" 2>/dev/null || true)
    fi
fi

if [ -z "$INTARIS_SESSION_ID" ]; then
    SESSION_FILE=$(state_file_for "$SESSION_ID")
    if [ ! -f "$SESSION_FILE" ]; then
        log "No state file, skipping recording"
        echo '{}'
        exit 0
    fi
    INTARIS_SESSION_ID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
fi

if [ -z "$INTARIS_SESSION_ID" ]; then
    log "No session_id in state file, skipping recording"
    echo '{}'
    exit 0
fi

# Look up this call's Intaris call_id by tool_use_id (set by
# intaris-evaluate.sh's PreToolUse call_id_map). Claude Code never actually
# sends a `call_id` field on PostToolUse — the field read above always
# comes back empty — so this is the real correlation path, replacing a
# previous heuristic that matched on tool name + args and mis-attributed
# results under Claude Code's routine parallel tool calls.
if [ -z "$CALL_ID" ] && [ -n "$TOOL_USE_ID" ] && [ -f "$SESSION_FILE" ]; then
    CALL_ID=$(jq -r --arg tid "$TOOL_USE_ID" \
        '(.call_id_map // []) | map(select(.tool_use_id == $tid)) | last | .call_id // empty' \
        "$SESSION_FILE" 2>/dev/null || true)
fi

build_headers

# Build tool_result event
EVENT_BODY=$(jq -n \
    --arg tool "$TOOL_NAME" \
    --arg call_id "$CALL_ID" \
    --argjson args "$TOOL_INPUT" \
    --argjson output "$TOOL_RESPONSE" \
    --argjson duration_ms "${DURATION_MS:-null}" \
    '[{
        type: "tool_result",
        data: {
            tool: $tool,
            call_id: $call_id,
            duration_ms: $duration_ms,
            args: $args,
            output: $output
        }
    }]')

log "Recording tool_result for: $TOOL_NAME"

# Output empty first, then send the event backgrounded (and disowned) —
# this hook's only job is this one fire-and-forget POST, so there is no
# reason to hold PostToolUse open for up to 2s waiting on it.
echo '{}'

curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -H "X-Intaris-Source: claude-code" \
    -d "$EVENT_BODY" \
    "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/events" >/dev/null 2>&1 &
disown
