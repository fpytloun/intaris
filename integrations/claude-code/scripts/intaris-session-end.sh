#!/usr/bin/env bash
# intaris session end hook for Claude Code
#
# Called on SessionEnd. Performs local cleanup of per-session temp state that
# must survive across Stop hooks so UserPromptSubmit can continue forwarding
# reasoning on later turns.
#
# Environment variables:
#   INTARIS_DEBUG - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

if ! require_jq; then
    log "jq is required for SessionEnd cleanup, skipping"
    echo '{}'
    exit 0
fi

INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ] || ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

SESSION_FILE=$(state_file_for "$SESSION_ID")

if [ -f "$SESSION_FILE" ]; then
    rm -f "$SESSION_FILE"
    log "Cleaned up session state: $SESSION_FILE"
fi

for child_file in "${INTARIS_STATE_DIR}/intaris_state_${SESSION_ID}_"*.json; do
    [ -e "$child_file" ] || continue
    rm -f "$child_file"
    log "Cleaned up child session state: $child_file"
done

echo '{}'
