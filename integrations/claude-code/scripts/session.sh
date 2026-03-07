#!/usr/bin/env bash
# intaris session hook for Claude Code
#
# Called on SessionStart. Creates an Intaris session via POST /api/v1/intention
# so that subsequent PreToolUse evaluations have a session to reference.
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_INTENTION  - Session intention override (default: auto-generated)
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_INTENTION="${INTARIS_INTENTION:-}"
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

# Generate deterministic Intaris session ID
INTARIS_SESSION_ID="cc-${SESSION_ID}"

# Session file for tracking across hook calls
SESSION_FILE="/tmp/intaris_session_${SESSION_ID}"

# Build intention
if [ -n "$INTARIS_INTENTION" ]; then
    INTENTION="$INTARIS_INTENTION"
elif [ -n "$CWD" ]; then
    INTENTION="Claude Code coding session in ${CWD}"
else
    INTENTION="Claude Code coding session"
fi

# Build request headers
HEADERS=(-H "Content-Type: application/json" -H "X-Agent-Id: $INTARIS_AGENT_ID")
if [ -n "$INTARIS_API_KEY" ]; then
    HEADERS+=(-H "Authorization: Bearer $INTARIS_API_KEY")
fi
if [ -n "$INTARIS_USER_ID" ]; then
    HEADERS+=(-H "X-User-Id: $INTARIS_USER_ID")
fi

# Build request body
BODY=$(jq -n \
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

log "Creating session: $INTARIS_SESSION_ID"

# Call POST /api/v1/intention (2s timeout to leave headroom)
RESPONSE=$(curl -s --max-time 2 \
    -X POST \
    "${HEADERS[@]}" \
    -d "$BODY" \
    "${INTARIS_URL}/api/v1/intention" 2>/dev/null || echo '{"error": true}')

# Check if session was created (or already exists — 409 is fine)
if echo "$RESPONSE" | jq -e '.ok' >/dev/null 2>&1; then
    log "Session created: $INTARIS_SESSION_ID"
    echo "$INTARIS_SESSION_ID" > "$SESSION_FILE"
elif echo "$RESPONSE" | jq -e '.detail' >/dev/null 2>&1; then
    # 409 conflict means session already exists — that's fine
    log "Session already exists or error: $(echo "$RESPONSE" | jq -r '.detail // "unknown"')"
    echo "$INTARIS_SESSION_ID" > "$SESSION_FILE"
else
    log "Failed to create session: $RESPONSE"
    # Still save the session ID — evaluate.sh will try lazy creation
    echo "$INTARIS_SESSION_ID" > "$SESSION_FILE"
fi

# Output empty (no modifications to Claude's behavior)
echo '{}'
