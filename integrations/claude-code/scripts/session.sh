#!/usr/bin/env bash
# intaris session hook for Claude Code
#
# Called on SessionStart. Creates an Intaris session via POST /api/v1/intention
# so that subsequent PreToolUse evaluations have a session to reference.
# Stores session state as JSON in a temp file for cross-hook communication.
#
# Environment variables:
#   INTARIS_URL        - Intaris server URL (default: http://localhost:8060)
#   INTARIS_API_KEY    - API key for authentication (required)
#   INTARIS_AGENT_ID   - Agent ID (default: claude-code)
#   INTARIS_USER_ID    - User ID (optional if API key maps to user)
#   INTARIS_INTENTION  - Session intention override (default: auto-generated)
#   INTARIS_ALLOW_PATHS - Comma-separated parent directories to allow reads from (e.g., ~/src)
#   INTARIS_DEBUG      - Enable debug logging to stderr (default: false)

set -euo pipefail

INTARIS_URL="${INTARIS_URL:-http://localhost:8060}"
INTARIS_API_KEY="${INTARIS_API_KEY:-}"
INTARIS_AGENT_ID="${INTARIS_AGENT_ID:-claude-code}"
INTARIS_USER_ID="${INTARIS_USER_ID:-}"
INTARIS_INTENTION="${INTARIS_INTENTION:-}"
INTARIS_ALLOW_PATHS="${INTARIS_ALLOW_PATHS:-}"
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

# State file for tracking across hook calls (JSON format)
SESSION_FILE="/tmp/intaris_state_${SESSION_ID}.json"

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

# Build allow_paths policy from INTARIS_ALLOW_PATHS (comma-separated directories)
# E.g., "~/src,~/projects" → ["/home/user/src/*", "/home/user/projects/*"]
POLICY_JSON="null"
if [ -n "$INTARIS_ALLOW_PATHS" ]; then
    ALLOW_PATTERNS="[]"
    IFS=',' read -ra AP_ENTRIES <<< "$INTARIS_ALLOW_PATHS"
    for ap_entry in "${AP_ENTRIES[@]}"; do
        ap_entry=$(echo "$ap_entry" | xargs)  # trim whitespace
        [ -z "$ap_entry" ] && continue
        # Expand ~ to home directory
        if [[ "$ap_entry" == "~/"* ]] || [[ "$ap_entry" == "~" ]]; then
            ap_entry="${HOME}${ap_entry:1}"
        fi
        # Ensure trailing /* for glob matching
        if [[ "$ap_entry" != *"*" ]]; then
            if [[ "$ap_entry" == */ ]]; then
                ap_entry="${ap_entry}*"
            else
                ap_entry="${ap_entry}/*"
            fi
        fi
        ALLOW_PATTERNS=$(echo "$ALLOW_PATTERNS" | jq --arg pat "$ap_entry" '. + [$pat]')
    done
    if [ "$(echo "$ALLOW_PATTERNS" | jq 'length')" -gt 0 ]; then
        POLICY_JSON=$(jq -n --argjson ap "$ALLOW_PATTERNS" '{"allow_paths": $ap}')
    fi
    log "Allow paths policy: $POLICY_JSON"
fi

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
elif echo "$RESPONSE" | jq -e '.detail' >/dev/null 2>&1; then
    # 409 conflict means session already exists — that's fine
    log "Session already exists or error: $(echo "$RESPONSE" | jq -r '.detail // "unknown"')"
else
    log "Failed to create session: $RESPONSE"
fi

# Write initial JSON state file (always, even on failure — evaluate.sh will retry)
# NOTE: Keep JSON schema in sync with evaluate.sh (lazy creation path)
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

# Output empty (no modifications to Claude's behavior)
echo '{}'
