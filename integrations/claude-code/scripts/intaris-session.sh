#!/usr/bin/env bash
# intaris session hook for Claude Code
#
# Called on SessionStart (startup, resume, clear, compact). Creates an
# Intaris session via POST /api/v1/intention so that subsequent PreToolUse
# evaluations have a session to reference. On resume/409, re-activates the
# existing session and updates its intention.
#
# Environment variables:
#   INTARIS_URL                 - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY             - API key for authentication (required)
#   INTARIS_AGENT_ID            - Agent ID (default: claude-code)
#   INTARIS_USER_ID             - User ID (optional if API key maps to user)
#   INTARIS_INTENTION           - Session intention override (default: auto-generated)
#   INTARIS_ALLOW_PATHS         - Comma-separated parent directories to allow reads from
#   INTARIS_DEBUG               - Enable debug logging to stderr (default: false)

set -euo pipefail

# Source shared library
. "$(dirname "$0")/intaris-lib.sh"

if ! require_jq; then
    log "jq is required for SessionStart, skipping"
    echo '{}'
    exit 0
fi

# Read hook input from stdin
INPUT=$(cat)

# Extract session info from the hook input
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
SOURCE=$(echo "$INPUT" | jq -r '.source // empty' 2>/dev/null || true)

if [ -z "$SESSION_ID" ]; then
    log "No session_id in hook input, skipping"
    echo '{}'
    exit 0
fi

if ! validate_session_id "$SESSION_ID"; then
    echo '{}'
    exit 0
fi

# Generate deterministic Intaris session ID
INTARIS_SESSION_ID="cc-${SESSION_ID}"

# State file for tracking across hook calls
SESSION_FILE=$(state_file_for "$SESSION_ID")

# Build intention
if [ -n "$INTARIS_INTENTION" ]; then
    INTENTION="$INTARIS_INTENTION"
elif [ -n "$CWD" ]; then
    INTENTION="Claude Code coding session in ${CWD}"
else
    INTENTION="Claude Code coding session"
fi

build_headers

# Build allow_paths policy
POLICY_JSON=$(build_allow_paths_policy)

# Build request body
BODY=$(jq -n \
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

log "Creating session: $INTARIS_SESSION_ID (source: $SOURCE)"

# Call POST /api/v1/intention (2s timeout to leave headroom)
RESPONSE=$(curl -s --max-time 2 \
    -w "\n%{http_code}" \
    -X POST \
    "${HEADERS[@]}" \
    -d "$BODY" \
    "${INTARIS_URL}/api/v1/intention" 2>/dev/null || printf '\n000')

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
RESP_BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    log "Session created: $INTARIS_SESSION_ID"
elif [ "$HTTP_CODE" = "409" ]; then
    # Session already exists — re-activate and update intention
    log "Session already exists, re-activating: $INTARIS_SESSION_ID"
    curl -s --max-time 2 \
        -X PATCH \
        "${HEADERS[@]}" \
        -d '{"status":"active"}' \
        "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}/status" >/dev/null 2>&1 || true
    curl -s --max-time 2 \
        -X PATCH \
        "${HEADERS[@]}" \
        -d "$(jq -n --arg cwd "$CWD" '{details: {source: "claude-code", working_directory: $cwd}}')" \
        "${INTARIS_URL}/api/v1/session/${INTARIS_SESSION_ID}" >/dev/null 2>&1 || true
else
    log "Failed to create session (HTTP $HTTP_CODE): $RESP_BODY"
fi

# Write initial JSON state file on first start, or preserve existing state on
# resume/clear/compact while refreshing the working directory.
if [ -f "$SESSION_FILE" ]; then
    acquire_lock "$SESSION_FILE" || true
    if [ -f "$SESSION_FILE" ] && jq -e '.session_id' "$SESSION_FILE" >/dev/null 2>&1; then
        STATE_JSON=$(jq \
            --arg sid "$INTARIS_SESSION_ID" \
            --arg cwd "$CWD" \
            '.session_id = $sid | .cwd = $cwd' \
            "$SESSION_FILE" 2>/dev/null)
        if [ -n "$STATE_JSON" ]; then
            write_state "$SESSION_FILE" "$STATE_JSON"
        fi
    fi
    release_lock "$SESSION_FILE"
else
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

# Output empty (no modifications to Claude's behavior)
echo '{}'
