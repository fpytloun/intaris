#!/usr/bin/env bash
# intaris stop failure hook for Claude Code
#
# Called on StopFailure (API error during a turn). Saves the last assistant
# message to the state file so the next UserPromptSubmit can include it as
# context for intention generation. Does NOT signal session completion
# (the session may resume after the error).
#
# Environment variables:
#   INTARIS_DEBUG - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

require_jq

# Read hook input from stdin
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ]; then
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

SESSION_FILE=$(state_file_for "$SESSION_ID")

if [ ! -f "$SESSION_FILE" ]; then
    echo '{}'
    exit 0
fi

# Extract last_assistant_message from hook input (may not be present on all errors)
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // empty' 2>/dev/null || true)

if [ -n "$LAST_MSG" ]; then
    # Truncate to 4000 chars to bound state file size
    LAST_MSG=$(printf '%s' "$LAST_MSG" | cut -c1-4000)

    acquire_lock "$SESSION_FILE" || { echo '{}'; exit 0; }
    if [ -f "$SESSION_FILE" ]; then
        UPDATED=$(jq --arg text "$LAST_MSG" '.last_assistant_text = $text' "$SESSION_FILE" 2>/dev/null || cat "$SESSION_FILE")
        write_state "$SESSION_FILE" "$UPDATED"
    fi
    release_lock "$SESSION_FILE"

    log "Saved assistant text from StopFailure (${#LAST_MSG} chars)"

    # Record assistant message event for session recording
    if [ "$INTARIS_SESSION_RECORDING" = "true" ]; then
        STOP_SID=$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)
        if [ -n "$STOP_SID" ]; then
            build_headers
            RECORD_BODY=$(jq -n --arg text "$LAST_MSG" \
                '[{type: "message", data: {role: "assistant", text: $text}}]')
            curl -s --max-time 2 \
                -X POST \
                "${HEADERS[@]}" \
                -H "X-Intaris-Source: claude-code" \
                -d "$RECORD_BODY" \
                "${INTARIS_URL}/api/v1/session/${STOP_SID}/events" >/dev/null 2>&1 || true
        fi
    fi
fi

# Output empty (StopFailure output is ignored by Claude Code)
echo '{}'
